# Финальная стабилизация и UX: контроль полноты

База: 7e65572, master, clean. Полное ТЗ: FINAL_STABILITY_AND_UX_SPEC.md. Новая нумерация не подменяет старые A–D IDs. Статус «—» означает, что проверка ещё не выполнена; это не NOT_REPRODUCED. Реальные записи, платежи, рабочие SQLite/env/session запрещены. Без субагентов.

| ID | Замечание | Статус | Причина / исправление / тесты |
|---|---|---|---|
| A1 | УСТРАНИТЬ ПУБЛИКАЦИЮ В ОБХОД ЕДИНОГО ЗАПУСКА | FIXED | RED2free+RED2paid;148passed и3paidUI. Free/PREMIUM DRAFT требуют ListingLaunch. Старые publish/payp/FSM безопасно перенаправляются/отклоняются; active promotion/native relist сохранены. Full gate pending |
| A2 | РЕДАКТИРОВАНИЕ НЕ ДОЛЖНО НЕЗАМЕТНО ЛОМАТЬ АВТОВЫДАЧУ | FIXED | Critical edits require explicit STOP and obligation checks; safe future after/greeting/Playerok reply preserve paid snapshots. test_final_critical_edits + test_final_future_messages14passed; full snapshot1993passed/3knownfailures subsequently fixed. |
| A3 | ЗАЩИТИТЬ УДАЛЕНИЕ ТОВАРА С НЕЗАВЕРШЁННЫМИ ЗАКАЗАМИ | FIXED | RED5;96passed профиль. Products scope guard в UI/preflight/dispatch, safe completed receipts, late lease/paid/Giftunknown/aliases. Live server status range не проверялся |
| A4 | СОГЛАСОВАТЬ РЕДАКТИРОВАНИЕ С УЖЕ ЗАРЕЗЕРВИРОВАННЫМИ ЗАКАЗАМИ | FIXED | Reserved/queued order snapshots unchanged during safe message updates; critical edits blocked for paid/reserved/unknown. test_stability_reserved_orders + test_final_stock_resume_authority + futuremessages actual worker receipts. |
| B1 | ОДИН СТАРЫЙ ЧАТ НЕ ДОЛЖЕН ОСТАНАВЛИВАТЬ НЕЗАВИСИМЫЕ ПРОДАЖИ | FIXED | RED launch/own-chat reply/polling; scoped orders/chat baseline, fresh identity preserved.102passed surrounding profile;24 bad candidates cannot starve healthy chat |
| B2 | ИСПРАВИТЬ BACKOFF РЕЗЕРВНОЙ СИНХРОНИЗАЦИИ ЗАКАЗОВ | FIXED | Real Synchronizer.run test: old-chat error keeps30s poll instead of120/900; orders failures and RetryAfter remain conservative |
| B3 | РАЗОБРАТЬ HTTP 403 ОТ НЕДОСТУПНОГО СТАРОГО ЧАТА | FIXED | SafeAccount/Playerok HTTP transport12cases: chat403 disables first, fresh viewer same account restores only desired permissions; viewer401/403/503/mismatch cannot restore |
| B4 | ПРАВИЛЬНАЯ ПЕРВИЧНАЯ ИНИЦИАЛИЗАЦИЯ УВЕДОМЛЕНИЙ | FIXED | Startup boundary persisted before sync. New serverdated sale/message survive inaccessible history/restart/recovery; historical backfill stays BASELINE;121passed profile. Missing serverdate cannot prove novelty and remains held during BACKFILL |
| B5 | ВОССТАНОВЛЕНИЕ АВТООТВЕТОВ ПОСЛЕ ВРЕМЕННОЙ ОШИБКИ | FIXED | RED2→42passed profile.3 bounded free-read attempts with durable deadlines; fresh rule/dialog before queue; existing outgoing event/unknown cannot retry |
| C1 | НЕ ДОПУСКАТЬ БЛОКИРОВКИ СКЛАДСКОЙ ВЫДАЧИ МЕДЛЕННЫМ TELEGRAM GIFT | FIXED | Controlled blocked MTProto RPC while stock/text deliver through same shared Outbox with2bounded serial resource lanes.73passed with agreements/dispatch/bundles; cancel/lease/late reply/STOP and one Stars payer covered;50orderload1passed. |
| C2 | ОПЛАЧЕННЫЕ ЗАБЛОКИРОВАННЫЕ ЗАКАЗЫ ДОЛЖНЫ БЫТЬ ВИДНЫ ВСЕГДА | FIXED | Paid obligations queried independently of notification status; actual Enrollment BLOCKED/NEEDS_FULFILLMENT and UNKNOWN chain quarantine visible. Home links orders; worker-cycle failures visible separately. |
| C3 | ДОЛГОВРЕМЕННЫЕ ОШИБКИ WORKER НЕ ДОЛЖНЫ БЫТЬ НЕВИДИМЫМИ | FIXED | Real caught chain errors3times produce durable cycle counter/one incident/Home diagnostic;79passed profile. Existing Supervisor fatal-task and Realtime ping/reconnect/read-only catchup regressions retained. Observational telemetry never authorizes retry. |
| C4 | НЕ ТЕРЯТЬ ВАЖНЫЕ УВЕДОМЛЕНИЯ ПРИ ОШИБКАХ TELEGRAM | FIXED | RED6 ->22passed outcomes/result/notifications. Migration033 preserves old UNKNOWN/view receipts. Telegram RetryAfter uses durable deadline; explicit400/403 REJECTED, timeoutUNCERTAIN no repeat;8case reopenDB regression. |
| C5 | ПРИОРИТЕТ КРИТИЧЕСКИХ УВЕДОМЛЕНИЙ | FIXED | Priority with reserved fair quotas and borrowed idle capacity;105UNKNOWN warnings bypass220oldself events while normal+secondary advance in same bounded200cycle.18passed notifications/preferences,36combinedprofile. |
| C6 | УВЕДОМЛЕНИЕ «СКЛАД ПУСТ» ДОЛЖНО ИСПОЛЬЗОВАТЬ ПРАВИЛЬНЫЙ STOCK_SCOPE | FIXED | Real relist/3reservations/restock RED1 ->30passed. Empty-stock notice checks Inventory shared scope AVAILABLE, obsolete warning skipped after replenishment without resuming sales. |
| C7 | БЕЗОПАСНОЕ ПЛАНИРОВАНИЕ ПРИ ДЛИТЕЛЬНЫХ ОПЕРАЦИЯХ | FIXED | Same bounded resource lanes/ThreadCalls/SQLite transaction ownership; independent Playerok delivery progresses while Stars waits, no parallel Stars. Existing lease/STOP/restart/UNKNOWN checks preserved;50paid-order workload proved50distinct deliveries. |
| D1 | ОБЪЕДИНИТЬ СОЗДАНИЕ И НАСТРОЙКУ TELEGRAM GIFTS | FIXED | Modern Wizard/Launch/GiftsPanel use LogicalProducts, catalog buttons and [XXX]; old unbound confirmation refused, historical bindings preserved. test_final_modern_gift_creation, Gift/UX/LP profiles74+41passed; final full gate pending. |
| D2 | ДОБАВИТЬ «ВЫДАЁТ PLAYEROK» В ЕДИНЫЙ МАСТЕР | FIXED | Fifth Wizard mode prepares verified PlatformOrders policy before publication; four reply/SENT combinations, first immediate PAID, STOP and UNKNOWN/restart. test_final_platform_creation8passed; profiles71/92passed. |
| D3 | ОДИН ПРОЦЕСС ПОДГОТОВКИ И ЗАПУСКА | FIXED | test_final_all_creation_modes four actual Wizard/remoteDRAFT/Launch/first-sale paths + platform fifth case; same PREPARE/COMMIT, no forged readiness, no post-publication arm. Fixture kwargs/recipient mistakes corrected,5passed with migrationprofile. |
| D4 | ИЗМЕНЕНИЕ БЛАГОДАРНОСТИ БЕЗ ОСТАНОВКИ ПРОДАЖ | FIXED | Future after/greeting/Platform paid reply supported without STOP; old paid/reserved/queued texts immutable, control/cutoff unchanged, stale STOP/text CAS rejects input.14passed suite/64surrounding. |
| D5 | ПРАВИЛЬНЫЕ КНОПКИ ИСПРАВЛЕНИЯ ПРОБЛЕМ | FIXED | test_final_diagnostic_actions: env help without fake toggle, platform recovery, individual Stars limits STOP/setup, stopped Gift menu. Profiles62+17passed; no external writes. |
| D6 | УБРАТЬ ЛОЖНОЕ ПРЕДУПРЕЖДЕНИЕ О ПОДКЛЮЧЕНИИ ЧЕРЕЗ ПЯТЬ МИНУТ | FIXED | test_ux_redesign actual Home: old verified identity is neutral with timestamp, not lost connectivity or fresh green. Connection/403 regression48passed; no permission mutation. |
| D7 | ПРАВИЛЬНАЯ ДЛИНА НАЗВАНИЯ С УЧЁТОМ [XXX] | FIXED | test_final_creation_names RED2 ->44passed surrounding profile. New input reserves6 UTF16 units,33bear emoji accepted/34refused; existing titled edit checks retained. |
| D8 | ПОНЯТНОЕ НАСЛЕДОВАНИЕ ПОСЛЕ ПЕРЕВЫСТАВЛЕНИЯ | FIXED | test_final_inheritance_choice RED1 ->36passed surrounding profile. Conservative defaultFalse, both consequences explicit; draft toggle never arms or publishes. |
| D9 | СОГЛАСОВАТЬ ВОЗОБНОВЛЕНИЕ СКЛАДА | FIXED | test_final_stock_resume_authority + reservedorders/scope/futuremessages40passed. Fresh LP identity/owner/control checks through commit_arm admission-only path; old epochs/reserves retained, sold aliases remain closed. |
| D10 | ПРАВИЛЬНАЯ КНОПКА ПЕРЕВЫСТАВЛЕНИЯ | FIXED | test_final_relist_visibility RED3 ->20passed with native relist/status/itempanel. True server permission and price required; missing proof is check-only, explicitFalse has no relist action. |
| D11 | ИСТОРИЯ ТОВАРА ДОЛЖНА ОТКРЫВАТЬСЯ БЕЗ ПУБЛИЧНОГО GET_ITEM | FIXED | test_final_local_item_history RED1 ->37passed surrounding profile. Actual Dispatcher local history works at public404 with zeroAPI calls; orders/operations/aliases retained, current card still fresh. |
| D12 | УБРАТЬ ДУБЛИРОВАНИЕ ТОВАРОВ В ОСНОВНОМ МЕНЮ | FIXED | test_final_product_overviews RED2 ->99passed profile. Automation/menu/Gifts share one owner/account-scoped logical representative, private snapshots excluded, legacy fallback and old history retained. |
| D13 | МАССОВЫЕ ОСТАНОВКИ ТОЛЬКО С ПОНЯТНЫМ ПОДТВЕРЖДЕНИЕМ | FIXED | test_final_mass_stop_confirmation RED4 ->210passed profile. Current owner/action/account/nonce/snapshot/expiry, durable consumed audit in atomic stop; stale/foreign/replay/restart covered. Old unsafe navigation exceptions removed. |
| D14 | УПРОСТИТЬ ОСНОВНЫЕ ЭКРАНЫ | FIXED | Launch messages are mode-specific: no irrelevant greeting/after-delivery for platform/manual, Gift default explicitly standard. RED2 ->35passed acceptance/regressionprofile; existing item diagnostics and deeper actions retained. |
| E1 | БЕЗОПАСНАЯ ОЧИСТКА СТАРЫХ РЕЗЕРВНЫХ КОПИЙ SQLITE | FIXED | Verified automatic snapshot retention latest10+current+up to3monthly; unknown/manual/corrupt/recent/symlink files untouched; new invalid snapshot blocks cleanup.3passed isolated backup/restore/primaryintegrity. Windows symlink branch depends on OS privilege. |
| E2 | ЗАВЕРШИТЬ ПОЛНЫЙ GITHUB ACTIONS | CONFIRMED | 0870063 Actions41584429708 SUCCESS.912db79 CI41600015072 3failed1993passed/857.32s; local same3failed1993passed/1375.69s. Root causes fixed and35passed profile. Final CI for c5cabfc or later still pending; no full success claimed. |

## Приёмочные сценарии

| № | Сценарий | Доказательство |
|---|---|---|
| 1 | Новый складской товар → подготовленный запас → попытка старой бесплатной публикации. | test_final_all_creation_modes stock case: actual Wizard/import/remoteDRAFT, old publish callback preserves stock intent and cannot publish until same Launch readiness/COMMIT. |
| 2 | Новый Telegram Gift → попытка публикации в обход настройки. | test_final_launch_source_intent: actual Wizard Gift → old free/paid entry; mode preserved, no queue bypass (109passed profile). |
| 3 | Новый ручной товар → корректная бесплатная публикация. | test_listing_launch + test_final_publish_authority: actual manual PREPARE/COMMIT and worker publication; no stock/Stars requirement. |
| 4 | Новый товар со встроенной выдачей Playerok → единый запуск. | test_final_platform_creation real Wizard/PlatformOrders/Launch/first-order workers; four optional action combinations, no duplicate goods. |
| 5 | Повторное нажатие старого publish callback. | test_dispatch_recovery concurrent publish and test_final_publish_authority old FSM refusal; one real transport mutation. |
| 6 | Потерянный ответ публикации. | test_listing_launch::test_publication_failure_and_unknown_never_republish; UNKNOWN never queues another publication. |
| 7 | Изменение условий между preview и dispatch. | test_listing_launch::test_changed_authority_or_price_invalidates_confirmation; fresh preflight remains mandatory. Agreement version acceptance guards in test_auto_agreements; full snapshot contains these without failures. |
| 8 | Активный складской товар → изменение описания. | test_final_critical_edits active description input requires explicit STOP; no projection side effect. |
| 9 | Активный товар → изменение характеристик. | test_final_critical_edits active editfields callback blocked before schema input. |
| 10 | Изменение названия с сохранением [XXX]. | test_title_code_runtime::test_dispatcher_configure_restart_and_rename_keeps_title_code; existing protected title readback/CAS. |
| 11 | Изменение благодарности без STOP. | test_final_future_messages14cases: stock/text/Gift after, greeting, platform paid reply; actual old/new worker sends and unchanged controls. |
| 12 | Изменение текста уже поставленного в Outbox сообщения. | test_final_future_messages old queued payload exact equality, old/new message transport assertions; unknown output never requeued. |
| 13 | Оплаченный заказ во время редактирования. | test_final_critical_edits::test_payment_after_edit_queue_prevents_mutation_without_destroying_reserve. |
| 14 | Запоздалый ответ после изменения ревизии. | test_final_critical_edits late-arm/UNKNOWN-restart and test_dispatch_recovery late receipt stays independent evidence. |
| 15 | Изменение критического параметра другим клиентом Playerok. | test_final_paid_attention external description change→actual Enrollment BLOCKED, no delivery. |
| 16 | Безопасное повторное подтверждение привязки. | test_final_critical_edits actual STOP→edit→readback→rebind preview/commit→worker→arm; same product/code/stock. |
| 17 | Удаление пустого черновика. | test_final_delete_obligations safe draft variant; actual Dispatcher confirmation/worker. |
| 18 | Удаление товара без активных заказов. | test_final_delete_obligations safe completed variants; history/reservations unchanged. |
| 19 | Удаление при PAID. | test_final_delete_obligations paid + late-paid preview/read/dispatch cases. |
| 20 | Удаление при RESERVED. | test_final_delete_obligations reserved variant; same reservations remain. |
| 21 | Удаление при ACTIVE. | test_final_delete_obligations queued active delivery variant. |
| 22 | Удаление при UNCERTAIN. | test_final_delete_obligations unknown stock and unknown delete block repeat. |
| 23 | Удаление при незавершённой операции Gifts. | test_final_delete_obligations unknown Stars payment remains blocking even after CONFIRMED deal. |
| 24 | Удаление после завершения всех обязательств. | test_final_delete_obligations complete/reminder variants require exact succeeded step receipts. |
| 25 | Запоздалый результат удаления. | test_final_delete_obligations late receipt after lease recovery remains UNCERTAIN; no replay. |
| 26 | Старый чат возвращает HTTP 503, новая продажа продолжается. | test_stability_subsystem_isolation actual Gift worker continues new paid sale under unrelated chat503. |
| 27 | Старый чат возвращает HTTP 403, viewer подтверждает аккаунт. | test_final_chat_identity HTTP boundary: get_chat/get_chat_messages403→fresh same-account viewer→restore only desired flags. |
| 28 | Старый чат возвращает HTTP 403, viewer возвращает HTTP 401. | test_final_chat_identity viewer401/403/503/mismatch keep writes disabled. |
| 29 | Действительная потеря авторизации. | test_playerok_connection + test_stability_subsystem_isolation genuine auth loss disables writes; included in35passed profile. |
| 30 | WebSocket не доставляет оплату, polling обнаруживает её. | test_universal_delivery_e2e actual polling Synchronizer sees PENDING->PAID independently of WebSocket; test_sync_recovery head polling/checkpoint/restart. |
| 31 | Исторический чат постоянно недоступен, polling новых заказов не увеличивается до 900 секунд только из-за него. | test_final_sync_isolation actual Synchronizer.run sleep remains30s for old chat failure (not120/900). |
| 32 | Восстановление после временной деградации. | test_final_notification_baseline actual later chat recovery + restart; test_final_auto_reply_retry free read recovery. |
| 33 | Заказы не обрабатываются при неподтверждённой идентичности. | test_playerok_connection native write transport cannot dispatch after unknown/wrong identity; fresh same-account viewer required. |
| 34 | notifications:initialized=False. | test_final_notification_baseline starts uninitialized with durable startup boundary. |
| 35 | Ошибка старого чата. | test_final_notification_baseline old history503; new stream remains operational. |
| 36 | Новое сообщение покупателя. | test_final_notification_baseline confirmed new buyer message creates one active ChatCard. |
| 37 | Новая оплаченная сделка. | test_final_notification_baseline new PAID and earlier-PENDING→PAID transition notify during backfill. |
| 38 | Последующее восстановление старого чата. | test_final_notification_baseline old chat recovers; boundary survives worker reconstruction. |
| 39 | Новые события не превращаются в исторический BASELINE. | test_final_notification_baseline old/new notification statuses remain correctly separated after globalOK. |
| 40 | Историческая переписка не засыпает владельца уведомлениями. | test_final_notification_baseline historical dated chat message is BASELINE; legacy initialized DB regression preserved. |
| 41 | Временный HTTP 503 на проверке чата. | test_final_auto_reply_retry temporary HTTP503 creates no outgoing operation or terminal failure on first free check. |
| 42 | Безопасный повтор после восстановления. | test_final_auto_reply_retry real fresh verification→single Outbox send after durable backoff/restart. |
| 43 | Сообщение уже удалено. | test_auto_replies::test_fresh_chat_or_message_mismatch_never_sends missing remote message case; no outgoing operation. |
| 44 | Диалог продолжился до повторной проверки. | test_final_auto_reply_retry continued dialog during backoff cancels answer. |
| 45 | Правило изменилось или отключено. | test_auto_replies::test_stop_while_waiting_api_lock_blocks_auto_send and fresh rule revision checks; existing full snapshot regression. |
| 46 | Неизвестный результат отправки не повторяется. | test_auto_replies unknown-send guard + test_final_future_messages UNKNOWN stock/Stars regression; no replay. |
| 47 | Массовая остановка правил требует подтверждения. | test_auto_replies real Dispatcher uses visible current stop confirmation; no send, old arming nonce remains invalid. |
| 48 | Медленный Gift RPC на 90 секунд. | test_final_outbox_resource_lanes uses event-controlled blocked payment under existing90sproductiontimeout. Not a90swall-time/live measurement. |
| 49 | Одновременная независимая складская продажа. | test_final_outbox_resource_lanes actual independent stock transport confirms before blocked Gift released. |
| 50 | Одновременная продажа постоянного текста. | test_final_outbox_resource_lanes actual independent fixed-text transport confirms before blocked Gift released. |
| 51 | Два Gifts с потенциальным списанием Stars. | test_final_outbox_resource_lanes second paid Gift cannot overlap SPENDING; existing concurrent Gift guards in test_gifts. |
| 52 | Два заказа на последнюю единицу склада. | test_stock_auto_cycle::test_two_database_connections_cannot_reserve_last_unit_twice uses two SQLite connections; test_final_paid_load50unique reservations and transport deliveries. |
| 53 | Перезапуск во время исполнения. | test_dispatch_recovery abrupt process exit preserves reservation and blocks duplicate; resource-lane reopen retains terminalUNKNOWN. |
| 54 | STOP при активных задачах. | test_final_outbox_resource_lanes STOP while dispatched payment retains confirmed delivery and stops new purchase; local owner STOP/rearm reserved regressions. |
| 55 | Потеря lease. | test_final_outbox_resource_lanes expiredlease during payment ->UNKNOWN, no repeat. |
| 56 | Запоздалый ответ. | test_final_outbox_resource_lanes late reply after lease recovery recorded without overwritingUNKNOWN; test_dispatch_recovery receipt UI. |
| 57 | Карантин UNCERTAIN. | test_final_future_messages unknown inventory send stays QUARANTINED after safe message update. |
| 58 | Отсутствие повторного платежа. | test_final_future_messages lost Stars RPC + restart remains one payment and UNCERTAIN. |
| 59 | Отсутствие двойной выдачи. | test_final_future_messages real workers old/new orders each receive one stock unit plus their pinned after text. |
| 60 | Проверка нагрузки с 50 одновременно оплаченными заказами. | test_final_paid_load actual UI stock import +50paid duplicate events ->50distinct receipts,50SUCCEEDED chains and no repeat.1passed20.89s. |
| 61 | Все пять типов товара создаются через основной мастер. | test_final_all_creation_modes manual/text/stock/catalogGift plus test_final_platform_creation fifth path; actual Wizard/Launch/worker. |
| 62 | Новые Gifts не используют независимый gift: путь. | test_final_modern_gift_creation six cases: new Gift identity/title code, catalog, modern list before arming, stale legacy confirm blocked. |
| 63 | Legacy-привязка сохраняется при обновлении. | test_gifts historical binding explicitly retained; rebind price change preserves old binding, existing receipts/UNKNOWN unchanged. |
| 64 | Дублирующая публикация отсутствует. | test_final_platform_creation unknown/restart no repeat; test_final_publish_authority/test_dispatch_recovery double click ->one external publication. |
| 65 | Старые кнопки не обходят новый контракт. | test_final_modern_gift_creation stale old binding confirm refused; old publish/payp callbacks use same launch, globalSTOP old-panel exemption removed. |
| 66 | Название длиной 72 UTF-16 с суффиксом проверяется до публикации. | test_final_creation_names UTF16 boundaries at real input; test_product_codes verifies final72-unit suffix contract. |
| 67 | При редактировании не теряется [XXX]. | test_title_code_runtime actual Dispatcher configure/restart/rename preserves same code. |
| 68 | Наследование выбирается явно. | test_final_inheritance_choice both consequences visible; no implicit arm/payment; existing arm this/inherit choices retained. |
| 69 | stock:resumeok согласует все разрешения. | test_final_stock_resume_authority and test_stability_reserved_orders: same LP/control/armed_at/three distinct reserved deliveries, stale STOP/critical edits rejected. |
| 70 | Запрещённое перевыставление не предлагается как доступное действие. | test_final_relist_visibility fresh may_be_published/price gating; test_quick_relist_status native publication guards retained. |
| 71 | История открывается при HTTP_404 публичного item. | test_final_local_item_history actual Dispatcher with public404, preserved local receipts and zeroAPIcalls. |
| 72 | После нескольких перевыставлений один LogicalProduct отображается в основном меню один раз. | test_final_product_overviews now tests two successive logical relistings before all3overview screens; historical orders retained. |
| 73 | Старые item_id сохраняются в истории. | test_final_product_overviews old confirmed order remains reachable from new alias local history. |
| 74 | Глобальные STOP требуют подтверждения. | test_final_mass_stop_confirmation + real ar/promo UI regressions: snapshot-bound confirmation and no implicit stop. |
| 75 | Устаревший callback не меняет состояние. | test_final_mass_stop_confirmation changed config/foreign owner/replayed confirmation; test_global_ux_finance obsolete STOP during unknown panel cannot mutate. |
| 76 | Обычный просмотр профиля не останавливает магазин. | test_playerok_connection::test_fresh_check_confirmed_binding_and_cached_status_not_authentication + profile dispatcher test; normal open uses cached runtime state, explicit check remainsfresh. |
| 77 | Подключение не становится ложно неисправным только из-за прошедших 300 секунд. | test_ux_redesign actual Home after one-hour old identity; neutral timestamp, no false reconnect and no API call. |
| 78 | WRITES_OFF, STARS_LIMIT и PROJECTION_MISMATCH ведут в правильные места. | test_final_diagnostic_actions actual env-help/platform/local-limit routes and direct correction controls; no writes. |
| 79 | Нет инструкций о старом коде в описании для новых товаров. | New Wizard/LogicalProducts text consistently says title[XXX]; historical gift-tag inspector explicitly remains legacy diagnostic, not new identification. |
| 80 | Пользователь может вернуться к товару после исправления проблемы. | test_final_diagnostic_actions actual per-item permission/platform/local-limits routes; limits STOP->reopen provides real editor, item backroute retained; general navigation regressions preserved. |
| 81 | Enrollment BLOCKED отображается на главном экране. | test_final_paid_attention actual BLOCKED Enrollment appears on Home with direct order callback despite no notification. |
| 82 | DeliveryChain BLOCKED отображается на главном экране. | test_final_paid_attention unknown stock transport makes actual chainBLOCKED and quarantine; Home/order link remains after Telegram notice timeout. |
| 83 | NEEDS_FULFILLMENT виден. | test_final_paid_attention actual delivered chain NEEDS_FULFILLMENT appears with completion action. |
| 84 | UNCERTAIN виден. | test_final_paid_attention unknown stock/send warning; test_global_ux_finance Home uncertain operation route; no resend. |
| 85 | Уведомление не доставлено, проблема остаётся видимой. | test_final_paid_attention actual notice transport timeout leaves local UNKNOWN/blocked chain visible in Home. |
| 86 | Worker постоянно ошибается, появляется сигнал. | test_final_worker_cycles real caught SQLite JSON fault3times→durable counter/one alert/Home/diagnostics; recovery clears active fault. |
| 87 | Обычная разовая временная ошибка не создаёт ложную критическую тревогу. | test_final_worker_cycles normal90s simulated cycle within180s deadline does not alarm; context observation spends nothing. |
| 88 | Критическое уведомление не голодает за большой очередью. | test_final_notification_priority105UNKNOWN behind220history events ->actual delivered warnings in first boundedcycle. |
| 89 | Некритические уведомления тоже когда-нибудь обрабатываются. | test_final_notification_priority ordinary history and secondary event progress alongside105critical events; unused quota borrowing preserves throughput. |
| 90 | Stock_empty учитывает stock_scope и перевыставления. | test_final_stock_empty_notice actual shared logical relist/3reserved/restock decision, no auto-resume. |
| 91 | Несколько перезапусков создают корректные копии. | test_final_backup_retention multiple Store.backup copies and startup_backup restored; actual production start not executed. |
| 92 | Политика очистки не удаляет нужные файлы. | test_final_backup_retention manual/corrupt/recent/solegood preserved; verified4oldcopies pruned; symlink cases only if OS allows creating them. |
| 93 | Основная SQLite остаётся целой. | selfcheck temporary WAL quick_check/foreign_key_check OK; working database never opened. |
| 94 | Сохраняемые копии можно открыть и проверить. | test_final_backup_retention and selfcheck reopen temporary backups; final selfcheck schema1..33 and136imports,0externalrequests. |
| 95 | Финальный offline pytest проходит целиком. | Не проверен в новом ТЗ |
| 96 | Финальный GitHub Actions завершается SUCCESS. | Не проверен в новом ТЗ |

## A1: граница текущего исправления

Неподготовленная бесплатная публикация запрещена worker-guard. publish: перенаправлен в LaunchPanel; старый FSM commit отклонён; конкурирующая кнопка убрана. Ручная публикация проверена через настоящие PREPARE/COMMIT, с проверкой DEFAULT0 и readback. PREMIUM/штатное перевыставление сохраняют прежние финансовые проверки; объединение PREMIUM-DRAFT остаётся следующим пунктом. Никаких live-операций. Независимого review нет (без субагентов).

### A1 / PREMIUM

PaidPromotions.queue отклоняет DRAFT без launch до резервирования расхода. Старые payp:item/pick/commit открывают существующий LaunchPanel в режиме PREMIUM с новой проверкой. Worker отклоняет ранее сохранённый неподготовленный PREMIUM-DRAFT. Разрешения на активное продвижение, штатное перевыставление, суммы, расходные резервы и UNKNOWN не ослаблены. Финансовые regression tests используют настоящее manual ListingLaunch для публикации, не подделку ready/marker.

## A3 / удаление

Read-only Products.deletion_problem / require_deletable сохраняет единую проверку обязательств по aliases одного LogicalProduct. UI даёт order/ops переход; неизвестные операции не повторяются. Старый nonce после отказа удаляется. Оплата после preview и во время fresh get_item блокирует RPC. После lease recovery поздняя квитанция сохраняется отдельно; основной UNCERTAIN не снимается. Stocks/reservations/history не удаляются. CLOSED+полные receipts снимают только устаревшее напоминание как причину запрета, не финансовые unknown/reserves.

Платформа: существующий removeItem(id), production proof — exact id+aware deletedAt. Список допустимых серверных статусов и атомарность с оплатой в другом клиенте без live API не объявляются доказанными; серверные ограничения не обходятся. Никаких реальных удалений.
