# Финальная adversarial-приёмка — 6 ноября 2026

## Граница результата

Проверка выполнена одним разработчиком, без субагентов. Исходный локальный HEAD: `0b32cb47efab55350c132fa77258ad4be1349a5b`, рабочее дерево было чистым. Это самостоятельные негативные проверки актуального кода, а не перенос прежних PASS.

**Вердикт: офлайн-приёмка PASS; полностью автономная эксплуатация24/7 НЕ ПОДТВЕРЖДЕНА.** Найденные десять адресных дефектов исправлены с RED/GREEN. Финальная регрессия:2465 passed,0 failed,0 errors,0 skipped. Нет утверждения об отсутствии всех возможных ошибок. Физический VPN, reboot/autostart, визуальный счётчик и настоящие платежи остаются за границей доказательства.

Рабочая SQLite, `.env`, сессии, разрешения, Happ и настройки Windows не изменялись. Внешних мутаций, платежей, публикаций и сообщений покупателям не было. Разрушающие сценарии используют временные SQLite и имитацию транспортной границы; реальны Dispatcher/FSM, guards, резервирование, Outbox и workers. Новых пользовательских кнопок, экранов, настроек и подтверждений нет.

## Карта зависимостей и охват

| Цепочка | Проверяемые production-компоненты | Доказательство и предел |
|---|---|---|
| PAID → обнаружение | `workers/sync.py`, `pagination.py`, `realtime.py`, `services/orders.py` | Изолированы неполные сделки/страницы и медленная история. API — доказательство оплаты, WS — только durable hint. |
| Допуск → резерв | `workers/enrollment.py`, `services/order_management.py`, `delivery_chains.py`, `logical_products.py` | Серверная дата, исходное разрешение, тот же продавец/товар/чат; отдельные RED для неполной сделки и лота. |
| Склад и текст | `Orders.reserve`, `DeliveryChains.create/dispatchable`, `OutboxWorker`, `Executor` | CAS/SQLite резерв, один секрет на заказ, неизвестная отправка не повторяется, отдельные lanes не обходят guard. |
| Telegram Gifts | `gifts/service.py`, `outcomes.py`, `worker.py`, `mtproto.py` | Плательщик, получатель, форма, стоимость, лимиты, резервы, пакет, started_at, RPC ACK и независимая квитанция. |
| Playerok-native | `services/platform_orders.py` | Площадка передаёт товар; бот не отправляет свой склад/текст повторно. |
| Доставка → завершение | `services/fulfillment.py`, chain steps, GiftWorker, PlatformOrders | Успешная передача и отметка выполнения — разные квитанции; обязательный отчёт не выдумывается. |
| Перевыставление | `workers/logical_products.py`, `LogicalProducts.adopt/require_delivery/pin_order`, `stock_scope` | Штатный relist, [XXX], один UUID, конфигурация и склад; коллизии/другой владелец не принимаются. |
| PREMIUM | `PromotionWorker`, `PromotionSettings`, `PaidPromotions`, `premium_review.py` | Точная позиция sequence, два наблюдения, конкретный тариф, согласие, бюджет, резерв, CAS, UNKNOWN. |
| Telegram UI | `router.py`, navigation context/state, callback ACK, SQLiteStorage, panel tracking | Полная регрессия настоящих Dispatcher/FSM-путей; интерфейс в этой работе не перестраивался. Не утверждается ручной просмотр каждой кнопки на живом боте. |
| Уведомления | `Notifications`, `OperationResults`, `HomePanel.paid_issues/problems`, logging | Приоритет критических событий, CAS, quota обычных, RetryAfter; неизвестную доставку уведомления нельзя доказать простым повтором. |
| Жизненный цикл | `__main__.py`, `Supervisor`, ThreadCalls, PlayerokConnection, system_tunnel, Telethon lifecycle | Owned tasks, cleanup, поздние результаты, повтор polling, независимость сервисов. Windows reboot/физический VPN проверяются отдельно. |
| Хранение | `Store`, миграции, backup_retention, UploadCleanup, InstanceLock, selfcheck | WAL, атомарные переходы, восстановление временной копии, ошибка каталога backup при реальном bootstrap. |

## Повторная оценка всех 15 исходных рисков

PASS IN TESTS означает выполнение указанного тестового сценария, а не доказанную круглосуточную эксплуатацию.

| № | Вывод актуального аудита | Проверка / решение |
|---|---|---|
| 1 | **Подтверждён и исправлен, P1.** Общий sync lock и последовательный цикл удерживали чтение новых оплат за историей. | `test_adversarial_sync_starvation.py`: RED2, затем дополнительный RED1 на невычитанное исключение owned chat task; GREEN в профиле33. В том же Synchronizer отдельное владение order/chat курсорами, один отменяемый и ожидаемый child, hints независимы. |
| 2 | **Подтверждён и исправлен, P1.** Ошибка одной записи останавливала проход страницы. | `test_adversarial_sync_pages.py`: RED4; сохраняется исходное evidence в Inbox, exact readback и настоящий identity conflict изолированы, курсор/следующие страницы продолжаются; GREEN35 смежных. |
| 3 | **Подтверждён и исправлен, P1.** Четыре проверки за30с не обеспечивали второе свежее наблюдение для50 лотов. | `test_adversarial_promotion_capacity.py`: RED2 —12/20 и0/50 покупок за тестовое окно. Адаптивный ограниченный batch4..20, elapsed-aware cadence, прежние интервалы свежести. GREEN30; реальные production scheduler/Outbox, тестовые списания по одному на20/50 разрешённых лотов. |
| 4 | **Подтверждён и частично устранён, P1.** PREMIUM preflight занимал единственный Playerok scheduler lane. | `test_adversarial_premium_starvation.py`: RED1; независимые stock/text отправляются до разблокировки чтения тарифа. GREEN50 с money/UNKNOWN/90s Gift регрессиями. Фактические Playerok POST по-прежнему строго сериализуются общим transport fence — небезопасно просто удалить lock. |
| 5 | **Подтверждено эксплуатационное ограничение.** `start.bat` — интерактивный однократный запуск, не внешний supervisor. | Подготовлена схема Task Scheduler ниже; НЕ установлена. InstanceLock защищает единственный процесс. Автозапуск/реальный reboot NOT VERIFIED. |
| 6 | **Подтверждён и исправлен, P1.** Missing user/category/obtaining/status отзывали Gift-разрешение; missing description давал KeyError и terminal failure. | `test_adversarial_gift_partial_listing.py`: RED5; typed preflight wait без оплаты и изменения enabled/revision. Дополнительно RED1: отсутствие RUB-цены всё ещё отзывает modern binding; современная проверка полноты исправлена, legacy не получает лишнего требования цены. После полного чтения/restart ровно один платёж. GREEN110 исходного профиля и57 окончательного. |
| 7 | **Консервативное окно существует; удалять fence нельзя.** После durable started/SPENDING и до фактического RPC процесс может погибнуть. | `test_final_acceptance_e2e.py::test_cancellation_immediately_after_durable_intent_never_retries_rpc`. После restart отсутствие сетевого ответа не доказывает NOT_DISPATCHED. UNKNOWN сохраняется. Сужение окна требует отдельного доказанного dispatch-boundary протокола; здесь платёжная архитектура не переписана. |
| 8 | **Подтверждён и исправлен, P1.** PermissionError backup прерывал запуск всех sale workers. | `test_adversarial_backup_startup.py`: RED1 на реальном application bootstrap. Wrapper проверяет quick_check и записываемость исходной SQLite, оставляет старые копии; существующий cleanup повторяет попытку. GREEN9. Непригодный исходный журнал не обходится. |
| 9 | **Не является доказанным избыточным запретом для текущего single-payer.** UNKNOWN Stars/refund блокирует новые траты того же плательщика. | Исходящий неизвестный расход/неподтверждённый остаток нельзя освобождать по одному UI balance. Exact ACK-receipt сверяется автоматически; нет корреляции — quarantine. `test_final_outbox_resource_lanes.py` доказывает продолжение независимых склада/текста после restart. Общая остановка всех способов не требуется. |
| 10 | **Подтверждён и исправлен, P2.** Health сверял лишь global revision, игнорируя account и scoped launch. | Два корректных RED в `test_adversarial_promotion_authority_health.py`; health использует existing has_authority/permissions. Запрет writes уже корректно давал DEGRADED — не переделывался. GREEN25. SCHEDULED означает разрешённый планировщик, а не выполненную покупку. |
| 11 | **Подтверждён и исправлен, P1.** HTTP/WS могли наследовать Windows proxy environment в direct mode. | `test_adversarial_proxy_environment.py`: RED2; `trust_env=False` для обоих Playerok транспортов, GREEN275. Это не доказательство внешнего российского IP, видимого Playerok. |
| 12 | **PARTIALLY VERIFIED.** Нативная привязка сокета проверена, полный VPN/DNS путь не доказан. | Сейчас happ-xray Up/index28, native socket option28, Bot getMe через system_tunnel=True успешен. Раньше index52: константа не зашита. Виртуальные10/60/300с + повторный обрыв покрыты tests/test_happ_recovery.py. Физический обрыв, DNS leak, packet capture, реальная смена индекса в активном платеже NOT VERIFIED. |
| 13 | **Частично проверено; универсальный READY не доказан.** ProductReadiness различает scope/blocking и owner intent, supervisor имеет health. | `test_product_readiness.py`, `test_readiness_promotion_isolation.py`, bootstrap/health тесты. BLOCKED одного заказа не должен объявлять STOP всему товару. Нет доказательства того, что каждый главный экран отражает каждую возможную отказавшую зависимость; нельзя выдавать общий зелёный тест за такое доказательство. |
| 14 | **Реальное ограничение неизвестной доставки уведомлений.** timeout после SendMessage не даёт безопасного права повторить отправку. | PROCESSING при restart → UNCERTAIN; RetryAfter отдельно. Уведомление может остаться недоказанно доставленным. Исходная оплаченная проблема остаётся в SQLite/заказах/существующем разделе проблем заказов, бизнес-операция не повторяется из-за Telegram. Абсолютной гарантии оповещения единственным неисправным каналом нет. |
| 15 | **Найдены и исправлены конкретные вечные блокировки, P1; STOP/реальные ревизии сохранены.** Missing core deal fields приводили к OWNER_REVIEW_REQUIRED. | `test_adversarial_incomplete_deal_core.py`: RED16; exact full-detail fallback, PENDING/retry до подтверждения; GREEN50, со складским резервом и без, текст, restart. Дополнительный listing-read RED8 описан ниже. |

## Дефекты и RED/GREEN

Полные файлы результатов находятся в `data/final-adversarial-*.txt` (локальные тестовые артефакты, не рабочая база). Выход GREEN0 означает успех процесса pytest, RED результаты не засчитываются как PASS.

| ID | Первопричина и последствия | Файлы / исправление | RED → GREEN |
|---|---|---|---|
| A11 P1 | Ambient proxy пересылал Playerok мимо выбранного OS маршрута | `app/playerok/adapter.py:SafeAccount.__init__`, `workers/realtime.py:connect` |2failed/1.52s →275passed/74.86s, exit 0 |
| A02 P1 | Per-entry exception обрывал pagination, скрывал следующую оплату | adapter.validate_page, workers/pagination.scan, sync.invalid_order; существующее notice rendering |4failed/17.13s →35passed/28.64s, exit 0 |
| A03 P1 | Capacity4/цикл давала stale вторую позицию | workers/promotions.py:once/run; bounded adaptive capacity |2failed/18.71s →30 passed/30.49s, exit 0 |
| A01 P1 | Медленная история держала order lock; failed child повторно выбрасывал исключение | workers/sync.py:once/process_requests/run/_start_chat_refresh |2failed/15.72s +1 failed/14.82s →33passed/29.26s, exit 0 |
| A06 P1 | Отсутствующее поле трактовалось как доказанная смена Gift-лота | gifts/service.py:inspect_item; Outbox preflight allowlist |5failed/20.61s + price RED1/12.75s →110 passed/101.08s и окончательный57passed/52.54s, exit 0 |
| A08 P1 | Исключение необязательной startup copy убивало приложение | backup_retention.attempt_startup_backup; __main__.run_connected; UploadCleanup.run |1 failed/23.36s →9passed/49.16s, exit 0 |
| A15 P1 | Неполный get_deal до deliverable попадал в ValidationError, навсегда BLOCKED | services/order_management.py:deal_details, Outbox retry allowlist |16failed/28.88s →50 passed/42.54s, exit 0 |
| A04 P1 | Paid preflight блокировал consumer всех остальных Playerok операций | __main__.outbox_loop: три bounded lane того же worker |1 failed/20.78s →50 passed/129.26s, exit 0 |
| A10 P2 | Неполный контракт health против фактической authority | workers/promotions.py:once; существующий PaidPromotions.has_authority |2реальных RED →25passed/24.15s, exit 0 |
| A15-L P1 | Неполный get_item stock/text: Conflict либо KeyError вместо ожидания | `LogicalProducts.fresh_delivery_item` |8failed/38.62s →28passed/91.80s, exit 0; сохранились реальные identity/title/STOP guards |

RED для A04 сначала выявлял недостаток тестового транспорта (multi-item update, raw_price), затем исправленный fixture воспроизвёл нужное starvation. Эти первые ошибки fixture не засчитаны доказательством production-дефекта. В новом A15 тесте отдельно считаются выдача и настроенная благодарность: два разных сообщения не считаются двойной выдачей. Первый черновик price-check слишком широко потребовал цену у legacy Gifts:15failed/42passed. Ошибка исправлена до коммита ограничением на modern binding; прежние safety assertions не изменены, профиль57passed. Старые тесты и их safety assertions не переписывались.

## Матрица блокировок: область, необходимость и восстановление

Это инвентаризация бизнес-guard семейств критического пути. Один guard может состоять из нескольких SQL/CAS предикатов. Она не заявляет формальную верификацию всех возможных исключений Python/OS. Для каждой строки указан необходимый защищаемый инвариант; неподтверждённые автоматические восстановления не обозначены PASS.

| Проверка / место | Область и условие | Видимость владельцу | Восстановление / автоматически? / вечность | Почему нужна; изоляция |
|---|---|---|---|---|
| `InstanceLock.__enter__` | Второй процесс на той же SQLite | Ошибка запуска | Освобождение OS lock после смерти первого; да | Два executor недопустимы; весь процесс |
| `Store` open/migrations/WAL | Непригодная схема, corrupt DB, невозможность commit | Ошибка запуска/worker, безопасный журнал | Исправление диска/восстановление проверенной копии; не подменять пустой DB | Нельзя отправлять без durable intent; весь процесс |
| `PlayerokConnection.check`, account binding | Подтверждённый другой аккаунт/401/blocked | Подключение/готовность | Временная сеть recheck30..300с; реальная identity требует владельца | Нельзя читать/писать как другой продавец; Playerok scope |
| `Executor.preflight`, SafeAccount.permit | Writes/automation запрещены или профиль не проверен | Причина в карточке/операции | Сеть восстанавливается автоматически; явный запрет нет | Защита мутаций; безопасные чтения не обязаны останавливаться |
| Sync health/orders:last_complete_sync | Нет актуального надёжного прохода оплат | Ожидание синхронизации | API polling + WS catch-up; да | Не выдавать на неполной истории; ошибки старого чата отделены |
| Page/cursor validation | Неверный cursor/envelope | Durable sync health | Повтор чтения с backoff; да, до исправления API | Нельзя тихо пропустить страницу; отдельная плохая запись теперь изолирована |
| `Orders.ingest`, identity/date conflict | Разные seller/buyer/item/chat/created_at в одной сделке | Проблема конкретного заказа | Временная неполнота дочитывается; настоящий конфликт вручную | Иначе можно выдать чужому; один заказ |
| `EnrollmentWorker`, armed_at/future_order | Историческая/недатированная продажа | Заказ требует проверки либо ждёт детали | Достоверная дата перечитывается; история до armed_at не допускается | Локальное время не право на старую продажу; один заказ |
| deal_details/deliverable | Нет флагов/чата/продавца/core snapshot | Фоновое ожидание заказа | Полный exact-deal read; существующий PENDING/retry; да | Нельзя резервировать/отправлять без получателя и paid proof |
| deliverable/platform_sale | Явно problem=True, иной direction/mode/status | Конкретная причина заказа | Проблема/возврат/выполнение не сбрасываются автоматически | Нельзя дублировать исполненное/вернувшееся; один заказ |
| `LogicalProducts.require_delivery` | Нет VERIFIED [XXX], owner/account, handler/config revision | Карточка товара/заказа | Новый подтверждённый состав — разрешение владельца; не авто | Код один не даёт права отправки; конкретный logical product |
| `LogicalProducts.verify_listing/fresh_delivery_item` | Изменены critical owner/category/obtaining/fields/code | Проблема привязки | Безопасные title/price/marketing auto-readback; настоящий конфликт нет | Не выдать старый подарок/чужой товар; один logical product |
| Relist scan/adopt/stock_scope | Несколько активных копий [XXX], чужой owner, неполный каталог | Ошибка привязки | Complete snapshot/background scan; безопасный relist автоматически | Нельзя выбрать случайную копию; один код/товар |
| logical_order_scopes | Другая конфигурация/исходный item/scope | Заказ/история | Оплаченные условия не переписываются ради новых прав | Резерв и состав закреплены за старым заказом |
| Local owner STOP/control_revision | Владелец остановил товар/магазин | Выдача остановлена | Только явное новое разрешение, не восстановление сети | Реальный обязательный запрет; границы команды STOP |
| Gift emergency STOP vs pause | gifts:stopped и enabled/revision; либо временная paused | Различимые состояния | Pause сохраняет намерение, снимается владельцем; STOP сам не снимается | Запрет денежных операций не обходится локальным arm |
| Stock availability / Orders.reserve | Нет AVAILABLE, последняя единица занята | Остаток/проблемный paid order | Пополнение и фоновые workers; да при прежнем разрешении | Один уникальный секрет на один заказ; остальные товары работают |
| stock_pause/stock_resume | Пусто; неподтверждённое снятие; нет доказательства bot pause | Одно существующее stock notice | Retry до POST; после unknown только readback; ручной pause не отменяется | Ошибка снять с продажи не разрешает oversell/новый резерв |
| `Store.claim/mark_started/finish` lease/CAS | Потерян lease, устаревший token, другой worker уже завершил | Операция/заказ | Unstarted восстанавливается; started→UNKNOWN | Нельзя поздно отправить после нового владельца lease |
| Late results / operation_outcomes | Поздняя квитанция конфликтует с решением | История/существующем разделе проблем заказов | Не автоматический resend; отдельная безопасная сверка | Сохраняет оба доказательства; scope операции |
| Outbox UNKNOWN | Нет доказанного исхода уже начатой мутации | Понятная причина операции | Edit/ACK читаются автоматически где достаточно корреляции; иначе quarantine | HTTP500/timeout не означают «ничего не было» |
| Chains step ordering | Предыдущий шаг не SUCCEEDED | Конкретный заказ | Подтверждённый receipt продвигает следующий; unknown не перепрыгивается | Не отправлять спасибо/fulfill до доказанной доставки |
| Gift personal payer/recipient | Сменился payer, username/userID, access hash | Gift order reason | Подтверждённые free reads; явный конфликт не guessing | Нельзя платить другому человеку; gift order/payer |
| Gift catalog/quantity/form | Недоступен gift, изменена цена/форма/состав | Ожидание/конкретный лимит | Safe refresh, прежний состав и per-order cap; да только unstarted | Не покупать другой подарок/цену без права |
| Gift per-order/daily/balance/reserves | Превышен заказ, сутки, нет баланса | Stars shortage/limit | Сутки сбрасываются сами; пополнение/чтение повторяются; cap не отключается сам | Учитываются незавершённые расходы/резервы |
| Gift SPENDING/UNKNOWN/refund | Общий payer уже занят/исход неизвестен | Gift покупки ждут сверки | Exact saved ACK→independent receipt автоматически; без evidence нет | Single-payer funds нельзя потратить дважды; stock/text не блокируются |
| Gift bundle prefix | Предыдущий gift не подтверждён/partial | Прогресс заказа/проблема | Только подтверждённый prefix; весь пакет не resend/refund | Один шаг не даёт права повторить пакет |
| Fulfillment proof/role/report | Нет delivery receipt; SYSTEM/native; нужен report/attestation | Причина именно заказа | Подтверждённая доставка + ранее разрешённое auto completion; обязательный отчёт вручную | Не подделывать выполнение/документы |
| PREMIUM authority/revision/quote | Нет согласия именно этого account/item/tariff, тариф изменён | Существующая карточка продвижения с per-item reason | Сеть перечитывается; новые расходы/состав требуют прежнего согласия в его границах | Не создавать право на деньги из policy.enabled |
| PREMIUM position/freshness/cooldown | Нет sequence/двух свежих замеров, позиция не хуже порога | Причина конкретного лота | Планировщик перепроверяет, now adaptive budget; да | Не покупать по выдуманной позиции/старыми замерами |
| PREMIUM budget/reservation/UNKNOWN | Не хватает доступных средств/лимитов; старый неизвестный расход | Конкретная блокирующая операция | Reset периода автоматически; unknown только доказанная сверка/существующий explicit review | Single-wallet money scope, не delivery scope |
| Native Telegram route / HappRoute | Туннель отсутствует/другой generation; старое соединение | Соединение восстанавливается | Close/drain, reconnect, bounded backoff; да | Не fallback на default route, не replay gift |
| Notifications CAS/UNKNOWN | Отправка могла пройти, ID неизвестен | Проблема всё равно сохранена в заказах | Known RetryAfter повторяется; unknown send не blind resend | Дубликат уведомления не должен создавать повтор бизнес-операции |
| Navigation/FSM token | Устаревший экран, другой message, более новый callback | Существующее уведомление об устаревшем действии | Актуальный существующий экран; не меняет разрешений | Нельзя поздним callback перезаписать соседнюю карточку/повторить покупку |
| Backup optional target | Каталог недоступен/ссылка/нельзя создать snapshot | Одна warning при отказе/восстановлении | Existing housekeeping retry60с; healthy source продолжает | Не удалять старые копии и не выдавать без записываемого журнала |

## Матрица восстановления и отрицательных сценариев

| Отказ | Что доказано изолированными production-путями | Что не доказано |
|---|---|---|
| Telegram/VPN10,60,300с + повторный обрыв | `test_happ_recovery`: bounded wait, запрет HTTP при down, восстановление polling и pinned connection | Физические обрывы этой машины/маршрутизация/DNS |
| Telethon handshake/read/paid RPC cancel | `test_mtproto_lifecycle`, `test_happ_recovery`: disconnect/drain до нового клиента, нет stale-close нового; paid остаётся UNKNOWN | Настоящая Stars-оплата при физическом обрыве |
| Playerok API outage, потеря WS | Sync/realtime recovery, сохранённые hints/cursors, повторные read-only, пропущенные paid после30мин/2ч | Реальный длительный outage и ограничения retention внешнего API |
| Slow get_chat_messages | Новые paid polling и exact deal hints проходят; actual stock/text Outbox отправляет до release history | Время догрузки многолетней истории при крайне медленном API |
| Malformed deal/page | Плохая запись не теряет следующую страницу, identity conflict сохраняется отдельно | Полностью недоступная/навсегда испорченная внешняя сделка |
| Оплата до полного detail | Нет резерва/send до evidence; повтор full read/restart продолжает исходный заказ | Все мыслимые новые формы GraphQL не доказаны |
| UNKNOWN send/payment | Durable intent/lease/late evidence; никаких слепых resend после любого restart | Без квитанции невозможно доказать успех |
| Saved ACK Gift | Existing GiftWorker read-only settle атомарно подтверждает Outbox и gift step, не снимает STOP | Без ACK/quarantined conflict автоматического решения нет |
| Два заказа на last stock | Уникальный CAS reserve, остальные ждут STOCK_EMPTY; пополнение не отменяет STOP | Реальные simultaneous buyers не использовались |
| Пустой склад, Playerok отказывает discontinue | existing `test_denied_native_pause_warns_once_and_never_reserves_extra_stock`: одна проблема, нет oversell | Реальное снятие живого лота запрещено условиями |
| PREMIUM preflight/MTProto90s | Изолированные scheduler lanes; independent stock/text продолжается | Медленный фактический Playerok POST остаётся сериализованным до transport timeout |
| Полное падение Python | `test_dispatch_recovery::test_abrupt_process_exit_preserves_reservation_and_prevents_resend` использует subprocess и временный журнал | Реальный kill действующего магазина не выполнялся |
| Worker/polling неожиданно вышел | Supervisor и bootstrap/polling network/normal-return recovery, single process | Не является Windows service/external watchdog |
| Backup отказ каталога | Application bootstrap продолжает paid stock, existing housekeeping повторяет | Сломанный исходный диск не лечится автоматически |
| SQLite migrations/restore | selfcheck, `test_fix_migrations`, verified backup reopen, old schema safety | Физический сбой питания/контроллера во время fsync |
| Диск переполнен/corrupt | Durable transaction не даёт права на отправку без commit; backup wrapper не маскирует повреждение источника | Заполнение настоящего системного диска не выполнялось |
| Перезагрузка Windows | Схема внешнего запуска подготовлена, InstanceLock есть | Задача не установлена; reboot/login/Happ startup/sleep не испытаны |

## Нагрузочные доказательства

- `test_adversarial_promotion_capacity`:20/50 выбранных лотов, sequence319, threshold35, полный production scheduler и Outbox, конкретные разрешённые quotes, один тестовый расход на лот. Виртуальная шкала30с, быстрый управляемый API; это проверка алгоритма ёмкости/свежести, не интернет-SLA.
- `test_final_paid_load`:50 подтверждённых paid событий с дублями →50 различных резервов →50 разных складских отправок правильным чатам, без повторов. Не считать это50 одновременно выполняемыми внешними POST.
- `test_delivery_load`:50/100 лотов, штатное перевыставление, HTTP429 и restart,50 продаж, сохранённый склад/UUID и отсутствие чужой выдачи.
- `test_final_outbox_resource_lanes`: реальный90-секундный timeout Gift на управляемом транспорте, независимый stock/text проходит раньше; UNKNOWN не повторяется.
- Локальные сроки4с/0.5с в тестах starvation — границы управляемого теста, не измерение p95 настоящих сервисов. В этой работе не получены новые p50/p95/p99 живого Telegram/Playerok и не заявлен SLA30/60с для нестабильного интернета.

## Сквозные способы выдачи: доказательства, не значение флага

| Способ / цепочка | Production-путь и тест | Статус офлайн / реальная граница |
|---|---|---|
| Telegram Gifts | `test_final_acceptance_e2e::test_complete_heart_sale_from_dispatcher_through_receipt_and_restart`, gift bundles/ACK recovery, real GiftWorker/Outbox/Sender на fake RPC | PASS IN TESTS после итогового полного прогона; реального Stars-платежа не было |
| Уникальный склад | `test_universal_delivery_e2e::test_ui_binding_relist_paid_delivery_and_thanks`, `test_final_paid_load`, last-unit/UNKNOWN/restart tests | PASS IN TESTS; проверяются реальные отправленные транспортом уникальные секреты и SQLite, не только запись флага |
| Постоянный текст | universal lifecycle, `test_logical_faults::test_paid_replays_poll_requests_and_restart_do_not_repeat_delivery_or_thanks`, partial detail/listing recovery | PASS IN TESTS; UNKNOWN текста не возвращается в очередь |
| Playerok-native | `test_playerok_delivery`: already marked, no second delivery, required report, unknown fulfill, два worker, STOP во время RPC | PASS IN TESTS; native receipt не заменяется предположением, отчёт не подделывается |
| Relist → следующий заказ | `test_logical_faults::test_same_and_new_item_id_relisting_keep_one_product_and_independent_orders`, snapshot/duplicate marker/relist load | PASS IN TESTS; новый item_id не меняет UUID/склад/состав |
|50 независимых лотов + потерянный ACK одного | `test_fifty_product_autonomy::test_fifty_independent_lots_keep_delivering_when_one_ack_is_lost` | PASS IN TESTS; ошибка одного заказа не превращает остальные безопасные продажи в глобальный STOP |
| PREMIUM2/20/50 товаров | runtime position/isolation + adversarial capacity + paid promotion tests | PASS IN TESTS; без настоящего списания и без доказанного visual two-card match |

Все перечисленные тесты входят в завершившуюся окончательную полную коллекцию; PASS относится к офлайн-транспорту, а не реальным платным операциям. Изменений обычных Telegram-маршрутов/кнопок нет. Увеличение обязательных действий владельца:0. Для подтверждённых временно неполных данных и отказа каталога backup восстановление выполняется без нажатий; исторические заказы, реальный STOP, новая финансовая область и неизвестный исход намеренно не превращаются в автоматическое разрешение.

## Реальная среда: только чтение

На6 ноября08:02UTC существующий Playerok get_item: `[TXE]` id `1f1d5440-c5be-6f00-8b87-73e35f7c6486`, APPROVED, sequence427, priority_position10; `[VPM]` id `1f1d8443-154e-68e0-4bb6-0420bf28abbb`, APPROVED, sequence418, priority_position10. Аккаунт создан без writes authority; SDK чтения закрыты корректно.

Рабочая SQLite дополнительно открыта через URI mode=ro, PRAGMA query_only=ON, без Store/migrations. У обоих кодов state=VERIFIED, enabled=1, gift_enabled=1, error_code=NULL, product revision=config_revision (TXE3, VPM2), approved_stars15. Global gifts:stopped=false, неизвестных gift_jobs0. **Снимки sync/connection/Stars относятся к5ноября17:33–17:36UTC, а не к текущему времени**; они не доказывают, что сейчас работает процесс или доступна новая выдача. Приложение не запускалось для проверки, поскольку запуск мог бы исполнить настоящие оплаченные операции.

Источник счётчика сайта повторно проверен6 ноября безопасным чтением актуального HTML и served production chunk `/_next/static/chunks/pages/products/%5Bslug%5D-e131581a98c334f5.js` (SHA256 `6154687950331837da8b59e7328ebcce62f12d17a3f4760d60fa9c0a3d1510da`): непосредственно `sequence / countItems`; никакой формулы из priority_position нет. См. `docs/PLAYEROK_POSITION_EVIDENCE.md`. Новые reads подтверждают существование distinct полей на двух живых лотах. **Новый визуальный two-card match не выполнен:** Computer Use прекратил доступ, поскольку не смог достоверно определить URL Chrome. Ограничение не обходилось. Это не PASS визуальной приёмки.

Happ сейчас Up, ifIndex28. Native HappRoute socket setsockopt/readback вернул28, Bot getMe с production system_tunnel admission успешен. Первичный диагностический вызов без system_tunnel и ошибочное ntohl при чтении socket option не использованы как доказательства; повторён корректный вызов с параметром приложения, сырое значение28 совпало. Ни getMe, ни option readback не доказывают внешний IP или отсутствие DNS leakage.

Read-only powercfg: сон от сети0 (disabled), от батареи0x4b0=1200с. Это текущие значения, не изменение системы. Батарейный сон остаётся эксплуатационным риском. Автозапуск проекта через Task Scheduler по исполняемому пути/аргументам не найден; это не исключает иной неизвестный владельцу способ запуска.

## Подготовленная схема внешнего запуска — НЕ УСТАНОВЛЕНА

`start.bat` оставлен интерактивным: бесконечный bat-loop после любого выхода мешал бы отличать явную остановку от падения и создавал бы restart storm. Безопаснее один Task Scheduler, напрямую контролирующий один `python.exe -u -m app`, с существующим application InstanceLock.

Готовый XML-шаблон находится в `docs/PLAYEROK_TASK_SCHEDULER_PREPARED.xml`. Он привязан к текущему checkout, использует interactive logon текущего владельца (Happ должен уже работать), IgnoreNew, restart только после ошибки1мин, максимум3попытки, без execution time limit. Файл подготовлен, не зарегистрирован. Он не даёт финансовых разрешений и не меняет `.env`/SQLite. До установки владельцем нужны dry-run с тестовым магазином, проверка normal stop/ошибочного выхода, повторного запуска и reboot. Если существующий InstanceLock занят, второй процесс не исполняет платежи. При постоянной порче БД не следует бесконечно перезапускать/пересоздавать журнал.

Windows до login и sleep/hibernate не заменяются этой схемой. Автоматическое включение после реального STOP не вводится. Реальное тестирование питания/перезагрузки и применение системных настроек требуют отдельного разрешения.

## Итоговая регрессия и SHA

Точный проверенный source/test HEAD: **`2a9266638bf76ef9bcd60b37d8f56772a36e1549`**. Последующий коммит отчётов не меняет app/tests/tools. Итоговый docs-only SHA указан в ответе; исполняемый код и тесты совпадают с проверенной версией.

Полный pytest выполнен штатным `tools/ci_pytest_shard.py --index N --count 12`: **2465 passed /0 failed /0 errors /0 skipped**, все 12 exit 0. Части:260,201,201,201,201,201,200,200,200,200,200,200. Проверены одинаковый HEAD во всех manifest,2465 уникальных непересекающихся nodeid и2465 уникальных XML testcase. XML-длительности частей78.62–193.33с, части параллельны; это не сетевой SLA магазина. Сохранён итоговый локальный артефакт `data/final-adversarial-acceptance.json`.

История отрицательных результатов не скрыта:

- Прогон630e428 (2456 тестов) прерван после дополнительного RED8; процессы остановлены до правки кода. Он не засчитан.
- Полный55949c7:2464 passed/0 failed; затем price RED1 выявил дополнительную ветвь A06.
- Полный3e4ec76:2464 passed/1 failed. Сбой нового A04 теста: транспортный поток уже добавил сообщение, worker ещё не обработал ответ/не записал inventory DELIVERED. Ожидание исправлено на фактическую отправку текста И durable receipt в том же лимите4с; production-код и safety assertions не ослаблены. Профиль GREEN1/6.87с, затем текущий полный2465 passed. Предыдущие manifests/XML-summary сохранены отдельно.

На этом же HEAD Ruff check, Ruff format --check (396 файлов), compileall app/tests/tools, pip check и selfcheck завершились exit 0. Selfcheck:144 production-модуля импортированы,32 migration-файла, схема1..33, SQLite backup reopened=true, external_requests=0. `test_fix_migrations`/backup retention/restore прошли в полном наборе. Prepared XML прочитан XML-парсером, но не установлен и не испытан Task Scheduler.

Четыре штатных PytestWarning о record_property с xunit2 не скрыты; метрики прочитаны из XML. Никакие падающие тесты не исключались, xfail/новые skip не добавлялись.

Офлайн-замеры Dispatcher (50 лотов,20 локальных переходов,5 карточек, контролируемая медленная история), миллисекунды:

| Этап | p50 | p95 | p99 |
|---|---:|---:|---:|
| Локальный переход |2.089|4.054|4.054|
| Callback ACK на fake Telegram |0.0734|0.1038|0.1143|
| FSM |0.0742|0.1171|0.1509|
| Navigation lock |0.0017|0.0029|0.0029|
| Playerok lock по callback-выборке |0|1.307|1.358|
| Playerok по callback-выборке |0|20.579|20.634|

Нули означают отсутствие соответствующего сетевого этапа в части локальных callback; это не нулевая задержка сервера. Telegram transport в fake boundary не моделирует реальное сетевое время, его нулевые значения не считаются измерением Telegram. Новых живых p50/p95/p99 не получено. В50-product autonomy тесте49 независимых заказов с одним потерянным ACK подтверждены за27.281 с wall-clock управляемого теста, max transport dispatch delay25.797 с, заданный read delay0.005 с. Это проверка алгоритма/очереди на управляемом транспорте, а не обещание доставки в интернете.

Удалённый GitHub CI на локальных новых SHA пока не выполнялся. Старый run94e723b с billing/spending-limit error не является SUCCESS и не подтверждает эту версию.

## Остаточные риски и критерий следующего шага

1. Визуальный sequence-match двух owner cards и packet/DNS/egress путь NOT VERIFIED; нельзя утверждать доказанный российский IP только по HTTP200.
2. Task Scheduler/reboot/физический Happ failover не применялись и не испытаны. Сейчас это не доказанная автономная Windows-установка24/7.
3. UNKNOWN без коррелированной квитанции требует настоящего решения: автоматизация не может безопасно выдумать исход. Single-payer Stars блокируется, независимые другие способы продолжаются.
4. Строго сериализованный фактический Playerok POST может задерживать остальные записи до своего ограниченного transport timeout; изолированы preflight чтения, не объявлена неограниченная параллельная запись.
5. Не доказаны все будущие формы неполного API и каждый внешний технический отказ. Отдельные class-level guards перечислены; отсутствие найденного дефекта не объявлено доказательством отсутствия ошибок.
6. Все платёжные/доставочные GREEN — офлайн. Настоящие покупки, Gift-send, native pause/relist/PREMIUM не проверялись и не разрешались этой работой.

Следующий разрешённый практический шаг после зелёной финальной регрессии — обычная загрузка проверенной версии, наблюдение sync/paid backlog/резервов/receipt/worker health и отдельно разрешённый controlled live test. Не включать новые бюджеты и не сбрасывать реальные UNKNOWN ради приёмки.


## Точные указатели проверяемого source HEAD

| Production guard / переход | Файл и строка |
|---|---|
| `SafeAccount.__init__` | `app/playerok/adapter.py:62` |
| `validate_page` | `app/playerok/adapter.py:638` |
| `PageScanner.scan` | `app/workers/pagination.py:15` |
| `Synchronizer.process_requests` | `app/workers/sync.py:46` |
| `Synchronizer.invalid_order` | `app/workers/sync.py:123` |
| `Synchronizer.once` | `app/workers/sync.py:168` |
| `Synchronizer._start_chat_refresh` | `app/workers/sync.py:177` |
| `Synchronizer.run` | `app/workers/sync.py:322` |
| `PromotionWorker.once` | `app/workers/promotions.py:80` |
| `PromotionWorker.run` | `app/workers/promotions.py:266` |
| `Realtime.connect` | `app/workers/realtime.py:41` |
| `GiftService.inspect_item` | `app/gifts/service.py:59` |
| `GiftService.runtime_reason` | `app/gifts/service.py:317` |
| `GiftService.guard` | `app/gifts/service.py:559` |
| `GiftService.execute` | `app/gifts/service.py:638` |
| `GiftService.reconcile` | `app/gifts/service.py:915` |
| `GiftService.recover_resource_preflights` | `app/gifts/service.py:971` |
| `PersonalGiftSender.pay` | `app/gifts/mtproto.py:324` |
| `GiftWorker.reconcile_due` | `app/gifts/worker.py:21` |
| `startup_backup` | `app/services/backup_retention.py:92` |
| `attempt_startup_backup` | `app/services/backup_retention.py:105` |
| `UploadCleanup.run` | `app/workers/cleanup.py:69` |
| `sale` | `app/services/order_management.py:11` |
| `deliverable` | `app/services/order_management.py:26` |
| `deal_details` | `app/services/order_management.py:104` |
| `LogicalProducts.verify_listing` | `app/services/logical_products.py:822` |
| `LogicalProducts.require_delivery` | `app/services/logical_products.py:1565` |
| `LogicalProducts.fresh_delivery_item` | `app/services/logical_products.py:1613` |
| `LogicalProducts.pin_order` | `app/services/logical_products.py:1647` |
| `DeliveryChains.create` | `app/services/delivery_chains.py:17` |
| `DeliveryChains.dispatchable` | `app/services/delivery_chains.py:282` |
| `PaidPromotions.permissions` | `app/services/paid_promotions.py:54` |
| `PaidPromotions.has_authority` | `app/services/paid_promotions.py:149` |
| `PaidPromotions.guard` | `app/services/paid_promotions.py:285` |
| `PaidPromotions.execute` | `app/services/paid_promotions.py:385` |
| `PlatformOrders.guard` | `app/services/platform_orders.py:249` |
| `PlatformOrders.verify` | `app/services/platform_orders.py:270` |
| `Fulfillment.load` | `app/services/fulfillment.py:80` |
| `PlayerokConnection.check` | `app/services/playerok_connection.py:97` |
| `PlayerokConnection.run` | `app/services/playerok_connection.py:182` |
| `ProductReadiness.snapshot` | `app/services/product_readiness.py:97` |
| `ProductReadiness.refresh_due` | `app/services/product_readiness.py:709` |
| `Store.claim` | `app/database/store.py:326` |
| `Store.mark_started` | `app/database/store.py:448` |
| `Store.finish` | `app/database/store.py:468` |
| `Store.recover` | `app/database/store.py:646` |
| `Store.backup` | `app/database/store.py:756` |
| `OutboxWorker.once` | `app/workers/outbox.py:979` |
| `outbox_loop` | `app/__main__.py:48` |
| `run_connected` | `app/__main__.py:118` |
| `HomePanel.paid_issues` | `app/telegram/home_panel.py:18` |
| `HomePanel.problems` | `app/telegram/home_panel.py:79` |
| `Notifications.once` | `app/workers/notifications.py:38` |
| `Notifications.run` | `app/workers/notifications.py:686` |
| `interface` | `app/telegram/system_tunnel.py:22` |
| `HappRoute.socket` | `app/telegram/system_tunnel.py:70` |
| `ThreadCalls.call` | `app/playerok/thread_calls.py:30` |
| `ThreadCalls.drain` | `app/playerok/thread_calls.py:94` |
| `InstanceLock.__enter__` | `app/security/instance.py:11` |


## Последующий инцидент 06.11.2026 12:14

После исходной приёмки установлен и исправлен реальный SDK persisted-query сбой способов получения, terminal preflight PREMIUM/edit и избыточная связь версии тарифа с версией бюджета. Подробности: [разбор инцидента](docs/INCIDENT_2026_11_06_PREMIUM_AND_EDIT.md). Финальный source/test HEAD e0b15bdc7617b07d81a09d2261fae3f2bc848014, полный pytest2485 passed/0 failed/0 skipped, quality/selfcheck зелёные. Прежние результаты2465 относятся к прежнему HEAD и не подменяют этот новый прогон. Реальных платных операций не было; эксплуатационные ограничения выше не сняты.
