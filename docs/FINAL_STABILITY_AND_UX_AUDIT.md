# Финальная стабилизация и UX: контроль полноты

База: 7e65572, master, clean. Полное ТЗ: FINAL_STABILITY_AND_UX_SPEC.md. Новая нумерация не подменяет старые A–D IDs. Статус «—» означает, что проверка ещё не выполнена; это не NOT_REPRODUCED. Реальные записи, платежи, рабочие SQLite/env/session запрещены. Без субагентов.

| ID | Замечание | Статус | Причина / исправление / тесты |
|---|---|---|---|
| A1 | УСТРАНИТЬ ПУБЛИКАЦИЮ В ОБХОД ЕДИНОГО ЗАПУСКА | FIXED | RED2free+RED2paid;148passed и3paidUI. Free/PREMIUM DRAFT требуют ListingLaunch. Старые publish/payp/FSM безопасно перенаправляются/отклоняются; active promotion/native relist сохранены. Full gate pending |
| A2 | РЕДАКТИРОВАНИЕ НЕ ДОЛЖНО НЕЗАМЕТНО ЛОМАТЬ АВТОВЫДАЧУ | CONFIRMED | Critical edits: RED5 behavioral cases →138passed profile; explicit STOP, obligation guards enqueue/dispatch, readback invalidates old approval, actual UI rebind/rearm. Future-only messages remain pending |
| A3 | ЗАЩИТИТЬ УДАЛЕНИЕ ТОВАРА С НЕЗАВЕРШЁННЫМИ ЗАКАЗАМИ | FIXED | RED5;96passed профиль. Products scope guard в UI/preflight/dispatch, safe completed receipts, late lease/paid/Giftunknown/aliases. Live server status range не проверялся |
| A4 | СОГЛАСОВАТЬ РЕДАКТИРОВАНИЕ С УЖЕ ЗАРЕЗЕРВИРОВАННЫМИ ЗАКАЗАМИ | CONFIRMED | Critical edit blocked for paid/reserved/late sale; reserve/history untouched, queued edit blocks rearm, unknown/restart no replay. Safe-message snapshot scenarios still pending |
| B1 | ОДИН СТАРЫЙ ЧАТ НЕ ДОЛЖЕН ОСТАНАВЛИВАТЬ НЕЗАВИСИМЫЕ ПРОДАЖИ | FIXED | RED launch/own-chat reply/polling; scoped orders/chat baseline, fresh identity preserved.102passed surrounding profile;24 bad candidates cannot starve healthy chat |
| B2 | ИСПРАВИТЬ BACKOFF РЕЗЕРВНОЙ СИНХРОНИЗАЦИИ ЗАКАЗОВ | FIXED | Real Synchronizer.run test: old-chat error keeps30s poll instead of120/900; orders failures and RetryAfter remain conservative |
| B3 | РАЗОБРАТЬ HTTP 403 ОТ НЕДОСТУПНОГО СТАРОГО ЧАТА | FIXED | SafeAccount/Playerok HTTP transport12cases: chat403 disables first, fresh viewer same account restores only desired permissions; viewer401/403/503/mismatch cannot restore |
| B4 | ПРАВИЛЬНАЯ ПЕРВИЧНАЯ ИНИЦИАЛИЗАЦИЯ УВЕДОМЛЕНИЙ | FIXED | Startup boundary persisted before sync. New serverdated sale/message survive inaccessible history/restart/recovery; historical backfill stays BASELINE;121passed profile. Missing serverdate cannot prove novelty and remains held during BACKFILL |
| B5 | ВОССТАНОВЛЕНИЕ АВТООТВЕТОВ ПОСЛЕ ВРЕМЕННОЙ ОШИБКИ | FIXED | RED2→42passed profile.3 bounded free-read attempts with durable deadlines; fresh rule/dialog before queue; existing outgoing event/unknown cannot retry |
| C1 | НЕ ДОПУСКАТЬ БЛОКИРОВКИ СКЛАДСКОЙ ВЫДАЧИ МЕДЛЕННЫМ TELEGRAM GIFT | — | Проверка ещё не выполнена |
| C2 | ОПЛАЧЕННЫЕ ЗАБЛОКИРОВАННЫЕ ЗАКАЗЫ ДОЛЖНЫ БЫТЬ ВИДНЫ ВСЕГДА | CONFIRMED | RED missing home attention for actual BLOCKED enrollment. SQL state-based paginated paid attention/order links, NEEDS_FULFILLMENT and closed-order exclusion covered. Worker degradation part depends on C3 |
| C3 | ДОЛГОВРЕМЕННЫЕ ОШИБКИ WORKER НЕ ДОЛЖНЫ БЫТЬ НЕВИДИМЫМИ | CONFIRMED | RED real caught chain failure→79passed profile; durable cycles/start/success/consecutive/retry/progress,1coalesced alert after3 failures,Home+diagnostics,recovery suppresses obsolete alert. Normal90sGift no alarm. Realtime extended coverage pending |
| C4 | НЕ ТЕРЯТЬ ВАЖНЫЕ УВЕДОМЛЕНИЯ ПРИ ОШИБКАХ TELEGRAM | — | Проверка ещё не выполнена |
| C5 | ПРИОРИТЕТ КРИТИЧЕСКИХ УВЕДОМЛЕНИЙ | — | Проверка ещё не выполнена |
| C6 | УВЕДОМЛЕНИЕ «СКЛАД ПУСТ» ДОЛЖНО ИСПОЛЬЗОВАТЬ ПРАВИЛЬНЫЙ STOCK_SCOPE | — | Проверка ещё не выполнена |
| C7 | БЕЗОПАСНОЕ ПЛАНИРОВАНИЕ ПРИ ДЛИТЕЛЬНЫХ ОПЕРАЦИЯХ | — | Проверка ещё не выполнена |
| D1 | ОБЪЕДИНИТЬ СОЗДАНИЕ И НАСТРОЙКУ TELEGRAM GIFTS | — | Проверка ещё не выполнена |
| D2 | ДОБАВИТЬ «ВЫДАЁТ PLAYEROK» В ЕДИНЫЙ МАСТЕР | — | Проверка ещё не выполнена |
| D3 | ОДИН ПРОЦЕСС ПОДГОТОВКИ И ЗАПУСКА | — | Проверка ещё не выполнена |
| D4 | ИЗМЕНЕНИЕ БЛАГОДАРНОСТИ БЕЗ ОСТАНОВКИ ПРОДАЖ | CONFIRMED | Future after implemented for managed text/stock/Gifts, RED3→9passed; immutable registered orders, real worker sends, unknown Stars/stock restart no repeat.119passed surrounding profile. Playerok reply and greeting routes still pending |
| D5 | ПРАВИЛЬНЫЕ КНОПКИ ИСПРАВЛЕНИЯ ПРОБЛЕМ | — | Проверка ещё не выполнена |
| D6 | УБРАТЬ ЛОЖНОЕ ПРЕДУПРЕЖДЕНИЕ О ПОДКЛЮЧЕНИИ ЧЕРЕЗ ПЯТЬ МИНУТ | — | Проверка ещё не выполнена |
| D7 | ПРАВИЛЬНАЯ ДЛИНА НАЗВАНИЯ С УЧЁТОМ [XXX] | — | Проверка ещё не выполнена |
| D8 | ПОНЯТНОЕ НАСЛЕДОВАНИЕ ПОСЛЕ ПЕРЕВЫСТАВЛЕНИЯ | — | Проверка ещё не выполнена |
| D9 | СОГЛАСОВАТЬ ВОЗОБНОВЛЕНИЕ СКЛАДА | — | Проверка ещё не выполнена |
| D10 | ПРАВИЛЬНАЯ КНОПКА ПЕРЕВЫСТАВЛЕНИЯ | — | Проверка ещё не выполнена |
| D11 | ИСТОРИЯ ТОВАРА ДОЛЖНА ОТКРЫВАТЬСЯ БЕЗ ПУБЛИЧНОГО GET_ITEM | — | Проверка ещё не выполнена |
| D12 | УБРАТЬ ДУБЛИРОВАНИЕ ТОВАРОВ В ОСНОВНОМ МЕНЮ | — | Проверка ещё не выполнена |
| D13 | МАССОВЫЕ ОСТАНОВКИ ТОЛЬКО С ПОНЯТНЫМ ПОДТВЕРЖДЕНИЕМ | — | Проверка ещё не выполнена |
| D14 | УПРОСТИТЬ ОСНОВНЫЕ ЭКРАНЫ | — | Проверка ещё не выполнена |
| E1 | БЕЗОПАСНАЯ ОЧИСТКА СТАРЫХ РЕЗЕРВНЫХ КОПИЙ SQLITE | — | Проверка ещё не выполнена |
| E2 | ЗАВЕРШИТЬ ПОЛНЫЙ GITHUB ACTIONS | CONFIRMED | Run41566409780 отменён после20m12s задания с лимитом20m; всеqualitystepsSUCCESS,pytestCANCELLED. Нужны timings/проверка ожиданий; timeout не менялся |

## Приёмочные сценарии

| № | Сценарий | Доказательство |
|---|---|---|
| 1 | Новый складской товар → подготовленный запас → попытка старой бесплатной публикации. | Не проверен в новом ТЗ |
| 2 | Новый Telegram Gift → попытка публикации в обход настройки. | test_final_launch_source_intent: actual Wizard Gift → old free/paid entry; mode preserved, no queue bypass (109passed profile). |
| 3 | Новый ручной товар → корректная бесплатная публикация. | test_listing_launch + test_final_publish_authority: actual manual PREPARE/COMMIT and worker publication; no stock/Stars requirement. |
| 4 | Новый товар со встроенной выдачей Playerok → единый запуск. | Не проверен в новом ТЗ |
| 5 | Повторное нажатие старого publish callback. | test_dispatch_recovery concurrent publish and test_final_publish_authority old FSM refusal; one real transport mutation. |
| 6 | Потерянный ответ публикации. | test_listing_launch::test_publication_failure_and_unknown_never_republish; UNKNOWN never queues another publication. |
| 7 | Изменение условий между preview и dispatch. | Не проверен в новом ТЗ |
| 8 | Активный складской товар → изменение описания. | test_final_critical_edits active description input requires explicit STOP; no projection side effect. |
| 9 | Активный товар → изменение характеристик. | test_final_critical_edits active editfields callback blocked before schema input. |
| 10 | Изменение названия с сохранением [XXX]. | Не проверен в новом ТЗ |
| 11 | Изменение благодарности без STOP. | test_final_future_messages: future after for stock/text/Gifts, old orders keep their text;9passed profile. Greeting/Playerok reply pending. |
| 12 | Изменение текста уже поставленного в Outbox сообщения. | Partial: test_final_future_messages asserts existing Outbox payloads unchanged; already queued AFTER-message-specific case still pending. |
| 13 | Оплаченный заказ во время редактирования. | test_final_critical_edits::test_payment_after_edit_queue_prevents_mutation_without_destroying_reserve. |
| 14 | Запоздалый ответ после изменения ревизии. | Не проверен в новом ТЗ |
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
| 30 | WebSocket не доставляет оплату, polling обнаруживает её. | Не проверен в новом ТЗ |
| 31 | Исторический чат постоянно недоступен, polling новых заказов не увеличивается до 900 секунд только из-за него. | test_final_sync_isolation actual Synchronizer.run sleep remains30s for old chat failure (not120/900). |
| 32 | Восстановление после временной деградации. | test_final_notification_baseline actual later chat recovery + restart; test_final_auto_reply_retry free read recovery. |
| 33 | Заказы не обрабатываются при неподтверждённой идентичности. | Не проверен в новом ТЗ |
| 34 | notifications:initialized=False. | test_final_notification_baseline starts uninitialized with durable startup boundary. |
| 35 | Ошибка старого чата. | test_final_notification_baseline old history503; new stream remains operational. |
| 36 | Новое сообщение покупателя. | test_final_notification_baseline confirmed new buyer message creates one active ChatCard. |
| 37 | Новая оплаченная сделка. | test_final_notification_baseline new PAID and earlier-PENDING→PAID transition notify during backfill. |
| 38 | Последующее восстановление старого чата. | test_final_notification_baseline old chat recovers; boundary survives worker reconstruction. |
| 39 | Новые события не превращаются в исторический BASELINE. | test_final_notification_baseline old/new notification statuses remain correctly separated after globalOK. |
| 40 | Историческая переписка не засыпает владельца уведомлениями. | test_final_notification_baseline historical dated chat message is BASELINE; legacy initialized DB regression preserved. |
| 41 | Временный HTTP 503 на проверке чата. | test_final_auto_reply_retry temporary HTTP503 creates no outgoing operation or terminal failure on first free check. |
| 42 | Безопасный повтор после восстановления. | test_final_auto_reply_retry real fresh verification→single Outbox send after durable backoff/restart. |
| 43 | Сообщение уже удалено. | Не проверен в новом ТЗ |
| 44 | Диалог продолжился до повторной проверки. | test_final_auto_reply_retry continued dialog during backoff cancels answer. |
| 45 | Правило изменилось или отключено. | Не проверен в новом ТЗ |
| 46 | Неизвестный результат отправки не повторяется. | test_auto_replies unknown-send guard + test_final_future_messages UNKNOWN stock/Stars regression; no replay. |
| 47 | Массовая остановка правил требует подтверждения. | Не проверен в новом ТЗ |
| 48 | Медленный Gift RPC на 90 секунд. | Не проверен в новом ТЗ |
| 49 | Одновременная независимая складская продажа. | Не проверен в новом ТЗ |
| 50 | Одновременная продажа постоянного текста. | Не проверен в новом ТЗ |
| 51 | Два Gifts с потенциальным списанием Stars. | Не проверен в новом ТЗ |
| 52 | Два заказа на последнюю единицу склада. | Не проверен в новом ТЗ |
| 53 | Перезапуск во время исполнения. | Не проверен в новом ТЗ |
| 54 | STOP при активных задачах. | Не проверен в новом ТЗ |
| 55 | Потеря lease. | Не проверен в новом ТЗ |
| 56 | Запоздалый ответ. | Не проверен в новом ТЗ |
| 57 | Карантин UNCERTAIN. | test_final_future_messages unknown inventory send stays QUARANTINED after safe message update. |
| 58 | Отсутствие повторного платежа. | test_final_future_messages lost Stars RPC + restart remains one payment and UNCERTAIN. |
| 59 | Отсутствие двойной выдачи. | test_final_future_messages real workers old/new orders each receive one stock unit plus their pinned after text. |
| 60 | Проверка нагрузки с 50 одновременно оплаченными заказами. | Не проверен в новом ТЗ |
| 61 | Все пять типов товара создаются через основной мастер. | Не проверен в новом ТЗ |
| 62 | Новые Gifts не используют независимый gift: путь. | Не проверен в новом ТЗ |
| 63 | Legacy-привязка сохраняется при обновлении. | Не проверен в новом ТЗ |
| 64 | Дублирующая публикация отсутствует. | Не проверен в новом ТЗ |
| 65 | Старые кнопки не обходят новый контракт. | Не проверен в новом ТЗ |
| 66 | Название длиной 72 UTF-16 с суффиксом проверяется до публикации. | Не проверен в новом ТЗ |
| 67 | При редактировании не теряется [XXX]. | Не проверен в новом ТЗ |
| 68 | Наследование выбирается явно. | Не проверен в новом ТЗ |
| 69 | stock:resumeok согласует все разрешения. | Не проверен в новом ТЗ |
| 70 | Запрещённое перевыставление не предлагается как доступное действие. | Не проверен в новом ТЗ |
| 71 | История открывается при HTTP_404 публичного item. | Не проверен в новом ТЗ |
| 72 | После нескольких перевыставлений один LogicalProduct отображается в основном меню один раз. | Не проверен в новом ТЗ |
| 73 | Старые item_id сохраняются в истории. | Не проверен в новом ТЗ |
| 74 | Глобальные STOP требуют подтверждения. | Не проверен в новом ТЗ |
| 75 | Устаревший callback не меняет состояние. | Не проверен в новом ТЗ |
| 76 | Обычный просмотр профиля не останавливает магазин. | Не проверен в новом ТЗ |
| 77 | Подключение не становится ложно неисправным только из-за прошедших 300 секунд. | Не проверен в новом ТЗ |
| 78 | WRITES_OFF, STARS_LIMIT и PROJECTION_MISMATCH ведут в правильные места. | Не проверен в новом ТЗ |
| 79 | Нет инструкций о старом коде в описании для новых товаров. | Не проверен в новом ТЗ |
| 80 | Пользователь может вернуться к товару после исправления проблемы. | Не проверен в новом ТЗ |
| 81 | Enrollment BLOCKED отображается на главном экране. | test_final_paid_attention actual BLOCKED Enrollment appears on Home with direct order callback despite no notification. |
| 82 | DeliveryChain BLOCKED отображается на главном экране. | Не проверен в новом ТЗ |
| 83 | NEEDS_FULFILLMENT виден. | test_final_paid_attention actual delivered chain NEEDS_FULFILLMENT appears with completion action. |
| 84 | UNCERTAIN виден. | Не проверен в новом ТЗ |
| 85 | Уведомление не доставлено, проблема остаётся видимой. | test_final_paid_attention local state, not Telegram notification status, determines attention. |
| 86 | Worker постоянно ошибается, появляется сигнал. | test_final_worker_cycles real caught SQLite JSON fault3times→durable counter/one alert/Home/diagnostics; recovery clears active fault. |
| 87 | Обычная разовая временная ошибка не создаёт ложную критическую тревогу. | test_final_worker_cycles normal90s simulated cycle within180s deadline does not alarm; context observation spends nothing. |
| 88 | Критическое уведомление не голодает за большой очередью. | Не проверен в новом ТЗ |
| 89 | Некритические уведомления тоже когда-нибудь обрабатываются. | Не проверен в новом ТЗ |
| 90 | Stock_empty учитывает stock_scope и перевыставления. | Не проверен в новом ТЗ |
| 91 | Несколько перезапусков создают корректные копии. | Не проверен в новом ТЗ |
| 92 | Политика очистки не удаляет нужные файлы. | Не проверен в новом ТЗ |
| 93 | Основная SQLite остаётся целой. | selfcheck temporary WAL quick_check/foreign_key_check OK; working database never opened. |
| 94 | Сохраняемые копии можно открыть и проверить. | selfcheck temporary backup reopens with schema1..32. Production backups not modified. |
| 95 | Финальный offline pytest проходит целиком. | Не проверен в новом ТЗ |
| 96 | Финальный GitHub Actions завершается SUCCESS. | Не проверен в новом ТЗ |

## A1: граница текущего исправления

Неподготовленная бесплатная публикация запрещена worker-guard. publish: перенаправлен в LaunchPanel; старый FSM commit отклонён; конкурирующая кнопка убрана. Ручная публикация проверена через настоящие PREPARE/COMMIT, с проверкой DEFAULT0 и readback. PREMIUM/штатное перевыставление сохраняют прежние финансовые проверки; объединение PREMIUM-DRAFT остаётся следующим пунктом. Никаких live-операций. Независимого review нет (без субагентов).

### A1 / PREMIUM

PaidPromotions.queue отклоняет DRAFT без launch до резервирования расхода. Старые payp:item/pick/commit открывают существующий LaunchPanel в режиме PREMIUM с новой проверкой. Worker отклоняет ранее сохранённый неподготовленный PREMIUM-DRAFT. Разрешения на активное продвижение, штатное перевыставление, суммы, расходные резервы и UNKNOWN не ослаблены. Финансовые regression tests используют настоящее manual ListingLaunch для публикации, не подделку ready/marker.

## A3 / удаление

Read-only Products.deletion_problem / require_deletable сохраняет единую проверку обязательств по aliases одного LogicalProduct. UI даёт order/ops переход; неизвестные операции не повторяются. Старый nonce после отказа удаляется. Оплата после preview и во время fresh get_item блокирует RPC. После lease recovery поздняя квитанция сохраняется отдельно; основной UNCERTAIN не снимается. Stocks/reservations/history не удаляются. CLOSED+полные receipts снимают только устаревшее напоминание как причину запрета, не финансовые unknown/reserves.

Платформа: существующий removeItem(id), production proof — exact id+aware deletedAt. Список допустимых серверных статусов и атомарность с оплатой в другом клиенте без live API не объявляются доказанными; серверные ограничения не обходятся. Никаких реальных удалений.
