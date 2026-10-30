# Функциональный аудит Playerok Control Center

Дата:2026-10-30, Europe/Moscow. Один функциональный проход после UX checkpoint7b44449.
Статус: один проход завершён; четыре воспроизведённые P0/P1 исправлены и проверены.

## Метод и границы доказательств

Основа: PROJECT_SPEC.md полностью прочитан в текущей сессии до этого прохода;
PROGRESS.md, ACCEPTANCE.md, TEST_REPORT.md и сохранённые live-свидетельства сопоставлены
с обработчиками, сервисами, воркерами, транспортом и БД. Статический inventory охватывает
все93 Python-модуля app:13209 строк/482 функций на момент начала итоговых проверок,
AST parsing и SHA256 в локальном data/functional_audit_production_inventory.json.
Это карта production scope, не доказательство выполнения каждой строки.
Проверялись функциональные переходы и критические взаимодействия. Pinned PlayerokAPI
используется существующим адаптером; библиотека целиком не переписывалась.

Использованы настоящий aiogram Dispatcher/SQLiteStorage/OwnerMiddleware, временная
SQLite WAL, тестовый Telegram BaseSession. Для новых транспортных гонок задействован
настоящий SafeAccount и httpx.MockTransport: проверяется отсутствие реального HTTP
mutation в самом guarded path. Read-модели в этих тестах нормализованно имитируются;
это не полный сервер Playerok. Остальные связанные бизнес-сценарии используют FakeAPI/
Marketplace. Отдельные существующие native GraphQL/REST contract tests дополняют их.
Никакой mock не является live-доказательством.

Повторный общий аудит после этого прохода не выполняется. Новые тесты добавлены только
для найденных взаимодействий; прежние подтверждённые изолированные проверки не
переписывались. Новый полный pytest нужен как итоговый regression check исправлений.
Реальных Playerok mutations, отправок покупателям, выдач и расходов в аудите0.

## Воспроизведённые P0/P1 и исправления

### FA-P0-01 — ручная выдача после локального возврата/проблемы

До исправления Executor.delivery проверял API, затем ждал API-lock. dispatch_guard
для нецепочечной выдачи проверял только lease/runtime и статус резерва. Если за это
время синхронизация сохраняла ROLLED_BACK/has_problem=true или владелец ставил локальную
проблему, createChatMessage всё равно уходил. Проверка цепочек эту ветку не защищала.

Доказательство до исправления: test_functional_audit.py::
test_manual_delivery_late_order_change_prevents_http,3 варианта — ожидались0 HTTP,
фактически зафиксирован createChatMessage. Постановка ручной выдачи прошла через
Dispatcher, а mutation — через настоящий SafeAccount/MockTransport.

Исправлено: свежая PAID-модель сохраняется Orders.ingest без отката terminal state;
при получении lock и непосредственно в transport guard снова проверяются локальный
PAID, seller/direction/problem/automation flags и локальная отметка проблемы.
Запрос не отправляется; резерв консервативно остаётся QUARANTINED, без возврата в запас.
Область: app/workers/outbox.py. Итог: FIXED_LOCAL, live не воспроизводилось.

### FA-P0-02 — ручная отметка выполнения после локального возврата

Fulfillment.load проверял сделку до ожидания serialization, но общая dispatch_guard
не проверяла актуальный локальный статус для fulfill. Тест
 test_fulfillment_late_refund_prevents_http до исправления зарегистрировал updateDeal
после сохранённого ROLLED_BACK. Это риск ошибочного исполнения возвращённой сделки.
Исправлено тем же локальным барьером непосредственно до HTTP. Семантика seller
attestation и readback Fulfillment не менялась. Итог: FIXED_LOCAL.

### FA-P1-01 — выход в раздел оставлял активным ввод другого сценария

Из stock:input переход в item/result/delivered/diagnostics не снимал FSM. Следующий
обычный текст мог попасть обратно в ввод склада, хотя пользователь видел другой экран.
Шесть вариантов test_leaving_input_flow_clears_sensitive_fsm воспроизведены до фикса:
первые четыре плюс sp:connections/stock:list, отдельно сохранённые в
data/functional_navigation_extended_before.xml (2 failed,4 passed до расширения фикса).
Исправлено: корневые переходы к карточке товара/заказа/чата, результатам, выданным,
профилю/статистике/диагностике/backup отменяют прежний input/confirmation context.
Сам сохранённый draft/stock/order не удаляется. Пагинация внутри сценария не заменена.
Область: app/telegram/router.py. Итог: FIXED_LOCAL.

### FA-P1-02 — положительная сверка последней выдачи не позволяла завершить заказ

После lost ACK outbox=UNCERTAIN, inventory=QUARANTINED, chain=BLOCKED. Owner UI сверял
message ID/seller/text/time; resolve делал outbox SUCCEEDED и inventory DELIVERED,
но оставлял chain BLOCKED. Store запрещал последующий Fulfillment, несмотря на
подтверждённые все шаги. В новом connected Dispatcher-тесте до фикса ожидалось
NEEDS_FULFILLMENT, фактически BLOCKED.

Исправлено внутри resolution transaction: только BLOCKED chain, только положительный
CONFIRM_MESSAGE и только если каждый зарегистрированный шаг уже SUCCEEDED — перейти
в NEEDS_FULFILLMENT либо SUCCEEDED по сохранённой политике. ACTIVE не выставляется,
непоставленные/неизвестные шаги не возобновляются, автоматический SENT не добавляется.
Тест проходит от синхронизации/склада до owner reconciliation и ручного SENT, с одной
отправкой товара. Область: app/services/reconciliation.py. Итог: FIXED_LOCAL.

Совместное доказательство до первых исправлений: data/functional_audit_before.xml —
8 failed; отдельная сверка до её фикса:data/functional_reconcile_before.xml —1 failed.
Эти падения намеренно сохранены как воспроизведение, не скрыты. После исправлений:
functional_audit_targeted.xml —62 passed; functional_audit_interactions.xml —106 passed;
functional_audit_business_flows.xml —3 passed. После расширения того же navigation fix:57 passed in13.53s,
data/functional_audit_final_targeted.xml. Итоговый full run приведён ниже.

## Функциональная матрица

Обозначения: LOCAL — подтверждено локально; LIVE-HIST — имеется прежнее live-доказательство,
в этом аудите не повторялось; PARTIAL — есть реализованная часть и явно указанный предел;
DRY — расчёт без исполнения. Доступность Telegram означает маршрут/handler, не наличие
разрешённых runtime flags. Изменение локального режима не включает глобальные разрешения.

| Функция / требование | Реализовано и доступно из Telegram | Проверено / основные доказательства | Проблемы и итог |
|---|---|---|---|
| Главное меню, owner/private access §2–3 | Да, inline/message middleware | foundation, telegram_integration, account_binding | LOCAL; first-start claim — прямое пользовательское исключение к первоначальному ТЗ |
| Навигация/FSM/старые callback §3 | Да, SQLiteStorage, nonce/revision | functional_audit, wizard_attachments, stock_ui | FA-P1-01 исправлен; LOCAL |
| Сохранение/возобновление локальных drafts §4 | Да, поиск/фильтры/pages/resume | drafts_panel, telegram_integration | LOCAL; отдельный draft не удаляется при выходе |
| Игры/категории/способ передачи §4 | Да, реальные API choices | category_contracts, full_preview_ui, item_category_picker | LOCAL+LIVE-HIST Stars; универсальность всех категорий live не доказана |
| Обязательные поля/условия §4 | Да, schema fields/agreements/snapshot | agreements, item_fields, products | LOCAL; неизвестные input types блокируются, не подменяются |
| Полное превью/исправление шага §4 | Да, pages/photos/masked stock | full_preview_ui | LOCAL; поля зависимой категории перепроверяются |
| Фото при создании §4 | Да, photo/document/preview/limits | wizard_attachments, transport_guards; LIVE_ACCEPTANCE_20261030.json | LOCAL+LIVE-HIST одного фото |
| Создание серверного draft §4 | Да, durable outbox, owner receipt | operation_results_ui, operations, full_preview_ui | LOCAL+LIVE-HIST; повторный callback не создаёт второй объект |
| Бесплатная публикация §4 | Да, quote DEFAULT0, отдельно от create | item_mutation_receipts, dispatch_recovery, connected audit flow | LOCAL+LIVE-HIST до APPROVED; moderation не объявляется активностью |
| Редактирование текста/атрибутов §6 | Да, preview/CAS/schema snapshot | edit_receipts, item_fields, operation_results_ui | LOCAL; произвольные поля отдельно live не приняты |
| Цена/настоящая скидка §4–6 | Да, immutable raw_price, validated price | discounts, placement_basis, connected audit flow | LOCAL+LIVE-HIST1000→749; увеличение базы опубликованного лота не обещается |
| Фото существующего лота §6 | Есть просмотр и replace-all одним фото | item_panel, edit_receipts | PARTIAL: нет отдельного редактирования каждого фото, P2-03 |
| Удаление §6 | Да, confirmation/own ID/deletedAt receipt | item_mutation_receipts, operations | LOCAL+LIVE-HIST404/list absence |
| Восстановление EXPIRED §6 | Нет самостоятельного restore handler | router/executor operation allowlist | GAP-D1: отдельная разработка с проверкой native server contract |
| Каталог/категория/цена/status/priority §6 | Да | item_filters, item_category_picker, listings_panel | PARTIAL: часть фильтров постраничная; P2-01 |
| Пагинация каталогов/диалогов §3 | Да, next/checkpoints/nonce | listings_panel, chat_panel | PARTIAL: нет previous для общего remote списка, P2-02 |
| Склад с карточки лота §8 | Да, четыре состояния и masked IDs | stock_ui, durability | LOCAL; секреты не выводятся в списки |
| Batch input/duplicates/item binding §8 | Да, preview/transaction/recheck | stock_ui, stock_auto_cycle | LOCAL; dedup в границах item_id |
| Однократный перенос из мастера §4/8 | Да, migration019/marker/disabled policy | full_preview_ui, stock_ui | LOCAL; rollback/competing import проверены |
| Конкурентный резерв §8–9 | Да, transaction+unique constraints | durability, stock_auto_cycle | LOCAL; единица не резервируется двум заказам |
| Empty/пополнение/явное resume §8 | Да, empty guard, resume CAS | enrollment, stock_ui | LOCAL; старые BLOCKED заказы автоматически не переигрываются |
| Политики/greeting/text/stock/after §8 | Да, UI+durable chain | automation, delivery_chains, telegram_integration | LOCAL; секретные тексты не показываются в карточках |
| Реальная readiness §8/14 | Да, единый DB snapshot | stock_ui, automation | LOCAL; сохранённый sync state не является мгновенной проверкой сети |
| Будущие PAID/order detection §9 | Да, polling+WS hints+baseline | enrollment, sync_recovery, realtime | LOCAL; WS handshake ранее live, новые реальные события не приняты |
| Ручная/автоматическая выдача §8–10 | Да, guarded outbox | functional_audit, stock_auto_cycle, delivery_chains | FA-P0-01 исправлен; live выдачи нет |
| Restart/lease/UNCERTAIN §9 | Да, quarantine/no blind replay | dispatch_recovery, thread_calls, durability | LOCAL; exactly-once на удалённой стороне не обещается |
| Безопасная owner reconciliation §9 | Да, IDs из истории чата и proof | functional_audit, reconciliation | FA-P1-02 исправлен для полностью подтверждённой цепочки |
| Карточка/поиск/история/problem заказа §10 | Да, cached fallback маркирован | order_management, catalog, telegram_integration | LOCAL+LIVE-HIST read; проблемная отметка локальная, не жалоба Playerok |
| Выдано—ожидает подтверждения §10 | Да, PAID+receipt+DELIVERED filter | operation_results_ui | LOCAL; outbox успех не заменяет status заказа |
| Seller Fulfillment §10 | Да, owner-attested/checklist/report | fulfillment, telegram_integration, functional_audit | FA-P0-02 исправлен; реальный SENT не принят |
| Автоматический SENT §8 | Нет; mark_sent ведёт к manual workflow | delivery_chains, settings_panel | GAP-D2; не считать описательную кнопку реализацией |
| Диалоги/история/фото §11 | Да, pages/media/revalidation | chat_panel, chat_images | PARTIAL: текст history обрезается; P2-04 |
| Ответ/шаблоны/фото/прочитанность §11 | Да, confirmation+outbox | replies, chat_images, telegram_integration | LOCAL; buyer send/upload/mark_read live не приняты |
| Уведомления/связь ответа §11 | Да, baseline/dedup/owner mapping | notifications, replies, notification_preferences | LOCAL; unknown Telegram send не повторяется |
| Терминальные результаты действий §3/4 | Да, owner receipts/menu/notifications | operation_results_ui | LOCAL; сохранённый server receipt, свежий read через карточку |
| Автоответы §14 | Да, exact phrase/future-only/limits | auto_replies, telegram_integration | LOCAL; не regex/AI, live не приняты |
| Калькулятор/вывод/комиссии §5/24 | Да, Decimal/provenance/estimate flags | finance, withdrawals, finance_panel | LOCAL+LIVE-HIST read; никаких настоящих выплат |
| Исторические расходы/прибыль §13 | Да, costs/allocations/review UI | order_finance, settlements | LOCAL; неизвестная себестоимость не ноль; P2-05 |
| Транзакции/сверка комиссии §13/24 | Да, independent ledger | transactions, settlements | LOCAL+LIVE-HIST462 tx/181 reconciled sales; withdrawal net неизвестен у части данных |
| Профиль/аватар/баланс §12 | Да, PNG+caption/fallback | profile, ui_and_rendering | LOCAL+LIVE-HIST read; изображение обновляется по открытию |
| Периоды/плавные графики/coverage §13 | Да, Moscow/real points | statistics, bestsellers, ui_and_rendering | LOCAL; неполная история не объявляется полной |
| Продвижение/ночь/позиция/budgets §7 | Да, настройки/worker dry-run | promotion_settings, promotion_worker, automation_queue | DRY; реальный платный executor отсутствует, GAP-D3 |
| Настройки/уведомления/диагностика §14 | Да, persisted prefs/CAS | notification_preferences, auto_replies, supervisor | LOCAL; secrets/runtime flags остаются в .env |
| Cleanup интерфейса/файлов §14/19 | Да, owner preview/reference checks | interface_cleanup, upload_cleanup | LOCAL; важные уведомления не цели cleanup |
| Backup/migrations/restore §15/19 | Backup/startup/button; restore вручную по README | installation/selfcheck/foundation | PARTIAL: нет Telegram restore/scheduled backup; P2-06 |
| Startup/shutdown/logging/account binding §2/17/19 | Да, lock/TLS/guards/supervisor | account_binding, thread_calls, transport_guards, foundation | LOCAL; длительный unattended soak не принят |

## Связанные пользовательские сценарии и рестарты

1. Создание→фото→stock transfer→policy arm→DEFAULT publication→name edit→900/749 discount:
   новый test_full_preview_ui::test_audit_connected_create_stock_policy_publish_edit_and_discounts.
   Настоящий Dispatcher, временная DB, mock remote state; dispatcher пересоздаётся
   между подтверждениями. Проверяется одно publish при повторном callback и обе цены.
2. PAID polling→reserve→delivery→receipt/notification→manual completion:
   stock_auto_cycle, test_mvp_sale_cycle, telegram_integration/fulfillment; новое
   продолжение с lost ACK ниже связывает все рабочие services/workers с owner UI.
3. Empty→replenish→explicit resume: stock_ui::test_reimport_never_reuses_reserved_delivered_or_quarantined_units,
   test_explicit_stock_resume_requires_confirmation_and_preserves_runtime_flags и
   test_stock_resume_does_not_reenable_after_last_unit_reserved_during_api_check;
   stock_auto_cycle проверяет crash/reopen, enrollment — следующий заказ без запаса.
4. Lost ACK→UNCERTAIN→owner proof→DELIVERED→ручной SENT:
   новый test_owner_confirmed_uncertain_last_delivery_can_finish_without_resend;
   одна send, одна fulfill, отдельные подтверждения владельца, без SQL-правки состояния.
5. Message→reply confirmation→notification/history update:
   telegram_integration::test_notification_reply_keeps_original_and_updates_after_durable_completion,
   chat_panel/replies tests. Для фото отдельно mock native upload/send path.
6. Sale→historical cost→expense allocation→review→profit:
   order_finance::test_dispatcher_cost_allocation_review_restart_double_commit_without_api;
   settlements сверяет реальные уравнения и неоднозначные источники. Не вводится текущая
   себестоимость задним числом.
7. Restart важного ввода: full_preview_ui, stock_ui, telegram_integration,
   item_fields, promotion_settings. Настоящий process crash/os._exit и reopening DB:
   dispatch_recovery и stock_auto_cycle. Не каждый API-контракт теста является native HTTP;
   transport_guards/fulfillment_transport отдельно проверяют проводную форму.

Факт восстановления Dispatcher с прежним SQLite FSM не равен убийству процесса.
В отчёте эти два уровня проверки намеренно различаются.

## Отложенные ограничения / P2 (без реализации)

- P2-01: поиск имени/price/priority в remote каталоге постраничный, не полный поиск магазина.
- P2-02: нет возврата к сохранённой предыдущей странице общего списка товаров/диалогов;
  карточки/главное меню могут сбрасывать прежний фильтр. Внутри chat history previous есть.
- P2-03: фото существующего лота — replace-all одним изображением; редактора галереи нет.
- P2-04: chat history обрезает сообщение до290 символов без раскрытия; нет отдельного
  unread-only фильтра/поиска общего списка диалогов.
- P2-05: историческая себестоимость вводится вручную, нет snapshot для будущих заказов;
  это неудобство, не обнаруженная арифметическая ошибка. Unknown profit честно остаётся unknown.
- P2-06: регулярные резервные копии/удобный restore требуют отдельной эксплуатации;
  кнопка backup и startup backup уже есть. Не отправлять DB с секретами в Telegram.
- P2-07: диагностика/некоторые списки используют технические имена/ID, нет сводного
  операционного dashboard. Для обычного запуска/сверки не требуется искать ID в логах:
  message IDs показаны в истории и operation receipts, manual resolution использует их.
- P2-08: stock UI не умеет отдельно списывать ошибочно загруженную AVAILABLE единицу;
  не править её произвольным SQL во время работающей выдачи. Это отдельная операция
  управления запасом, не основание автоматически выпускать QUARANTINED.
- P2-09: один UNCERTAIN может дать owner receipt и обязательный critical alert — две
  разные записи уведомления. Повторной отправки товара это не создаёт.

## Незавершённые части исходной спецификации — отдельная разработка

GAP-D1 — P1 по полноте ТЗ (§6, важно для полного управления каталогом): восстановление EXPIRED/SOLD не
имеет штатной кнопки/исполнителя. Нельзя безопасно заменить restore обычным publish:
тот разрешён только DRAFT. Требуется проверка конкретного native contract и отдельная
реализация, не скрытая подмена. В этом аудите новая возможность не добавлялась.
GAP-D2 — P1 по полноте автоматизации (§8): полностью автоматический seller SENT отсутствует. Ручной attestation
работает локально; buyer CONFIRMED бот не подделывает. Это не ошибка текущего manual mode.
GAP-D3 — P1 по полноте продвижения (§7): real paid promotion executor отсутствует; настройки/позиции/budgets
работают в dry-run. Наличие dry-run не закрывает требование реального исполнения.

Это незавершённые возможности исходного ТЗ, не новые воспроизведённые дефекты
существующего исполнителя. Их реализация требует отдельной разработки и выходит за
ограничение аудита «не добавлять новые возможности». Все три gap остаются открытыми
P1 по полноте ТЗ и не объявлены исправленными. Они не блокируют наблюдаемый бесплатный
stock MVP с ручным подтверждением, но не позволяют объявить исходное ТЗ выполненным.
Если в цепочке после положительной сверки ещё остались неотправленные/неизвестные
шаги, она остаётся BLOCKED: автоматического replay/skip UI нет. Исправление FA-P1-02
не расширяет разрешения и не обещает произвольное возобновление неизвестной цепочки.

## Live-подтверждения и противопоказания к заявлениям о готовности

Сохраняется прежний LIVE_ACCEPTANCE_20261030.json: один Stars lot с фото,
price1000/raw1000→749/raw1000, DEFAULT0, PENDING_APPROVAL→APPROVED, delete и новая
read-only session404/NOT_FOUND+list absence. Audit не повторял эти записи.
Не принимались live: stock send, сообщения/фото покупателю, mark_read, real paid order
в текущую pipeline, seller fulfillment, новые Telegram result notifications/все UI.
Auth/profile/catalog/category/order/chat/transaction reads были подтверждены ранее;
не требовалось повторять их для воспроизведения локальных гонок. Текущая бессрочная
валидность сессии и полнота истории этим отчётом не гарантируются.

## Противоречия экранов и действительного состояния

Исправлены stale input context и blocked-final-chain после positive resolution.
Сохранённые server receipts не названы текущим состоянием: актуальная карточка делает
read API. Ready status локальный и основан на policy/guard/baseline/runtime; сетевой
сбой показывается отдельно и может требовать новой сверки. mark_sent не auto SENT.
Технические GAP и P2 выше остаются видимыми, платное исполнение нигде не обещается.

## Итог

Оценка ежедневного удобства:7/10. Основные существующие сценарии связаны и локально
проверены; неопределённые исходы требуют внимания владельца. Для наблюдаемой live-
проверки складского MVP локальная готовность есть. Для полностью автономной реальной
эксплуатации ещё необходима согласованная настоящая продажа/выдача и длительная работа
под наблюдением. Количество тестов не переводится в процент готовности.

Предварительный полный прогон:739 passed in101.08s, data/functional_audit_full_results.xml.
Окончательный прогон после двух дополнительных navigation variants: 741 passed in102.72s, data/functional_audit_final_full_results.xml.
Ruff check/format160 files clean, pip check clean, offline selfcheck OK (92 imports,
19 migration files/schema1–20, SQLite backup/reopen, PNG). Secret scan tracked/unignored
files clean; .env/spec/data/logs ignored, runtime writes/automation false. Schema не
изменялась этим аудитом; рабочая БД не открывалась через Store и не мигрировалась.
Незакрытых воспроизведённых P0/P1 этих четырёх дефектов нет; это не доказательство
отсутствия любых скрытых ошибок. GAP-D1/D2/D3 и live-критерии остаются открытыми.
После записи итогов — остановиться. Второй общий проход, P2-реализации, новые
поставщики/платные функции этим аудитом не запускаются.


## Дополнительный проход по новому запросу пользователя —2026-10-30

Основание: после завершения первого аудита пользователь прямо попросил проверить ещё
и исправить новые найденные баги. Проверялись непокрытые взаимодействия, а не повторно
вся матрица. Новых возможностей/поставщиков/платных действий не добавлено.

### FA2-P0-01: повторное использование квитанции чужой операции

Reconciliation проверяла ID/sender/text/time сообщения, но не проверяла, связан ли
этот же message ID уже с другим outbox action. При одинаковом тексте в одном чате
одна реальная отправка могла подтвердить две разные неизвестные операции. Возможны
ложная отметка выдачи и неправильное освобождение карантина в DELIVERED.

До исправления воспроизведено4 тестовых случая: известный успешный результат, поздний
ответ другой операции, прежнее owner resolution и две параллельные owner resolution.
Оба concurrent подтверждения принимались; результат другого action не отклонялся.
Доказательство:data/audit_followup_before.xml (эти4 случая падают).

Исправлено: проверка known message IDs в outbox.result и outbox_late_results до показа
положительной evidence и повторно внутри BEGIN IMMEDIATE resolution transaction.
Собственный action исключён, его late receipt можно подтвердить. При конфликте
операция остаётся UNCERTAIN, stock остаётся QUARANTINED, никаких send/replay нет.
Дополнительный stock regression доказывает, что квитанция другого ответа не снимает
карантин выдачи. Это контроль уже известных связей, не обещание математического
exactly-once или автоматической привязки ещё не сохранённого серверного ответа.
Файл:app/services/reconciliation.py. Статус:FIXED_LOCAL.

### FA2-P0-02: постоянный текст уходил после появления проблемы в заказе

Предыдущий барьер ручной/stock delivery не покрывал delivery_text и chain_message.
DeliveryChains.dispatchable проверяла только локальный status='PAID', поэтому поздний
has_problem=true при том же PAID не останавливал текстовую выдачу. Native guarded
SafeAccount/httpx.MockTransport regression зафиксировал createChatMessage вместо0.
Доказательство:test_fixed_text_delivery_rechecks_server_problem_before_http до фикса
в data/audit_followup_before.xml.

Исправлено: все шаги цепочки перед отправкой дополнительно проходят deliverable по
текущему локальному payload (seller/direction/PAID/problem/server automation). Эта
проверка вызывается в dispatch guard, включая повтор после ожидания rate limit.
Файл:app/services/delivery_chains.py. Статус:FIXED_LOCAL.

### FA2-P1-01: owner result receipt считалась обычной панелью

Router сохранял оригинал сообщений из notifications, но не operation_receipts.
Callback из нового result notice заменял его на карточку/меню через editMessageText.
Также InterfaceCleanup не исключала operation_receipts, если сообщение уже было
зарегистрировано в panel_messages (например, предыдущей версией после его редактирования).

Два red regression: navigation ожидал sendMessage, получал editMessageText;
cleanup preview включал важный receipt. Доказательства:audit_followup_before.xml и
 audit_followup_cleanup_before.xml (отдельный1 failed).
Исправлено: preserve учитывает обе таблицы; cleanup исключает receipts как при preview,
так и при повторной проверке snapshot перед удалением. Stale cleanup plan отклоняется,
delete_message не вызывается. Файлы:router.py,services/interface_cleanup.py.
Статус:FIXED_LOCAL.

### Проверки и результат дополнительного прохода

До исправлений:6 failed в audit_followup_before.xml и1 failed в
 audit_followup_cleanup_before.xml; ошибки намеренно воспроизведены, не скрыты.
После:82 passed in19.48s (audit_followup_targeted.xml); после добавления проверки
stock quarantine47 passed in17.30s (audit_followup_final_targeted.xml).
Последний full suite:749 passed in91.12s, data/audit_followup_full_results.xml.
Ruff160 files чистые. Реальные Playerok/Telegram запросы, выдачи, платежи0;
использовались временные DB и mock transports. Миграции и Fulfillment semantics
не менялись. Ранее открытые GAP-D1/D2/D3 и live acceptance остаются открытыми.
После сохранения проверенного checkpoint этот дополнительный проход завершается.
