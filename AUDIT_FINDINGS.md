# AUDIT_FINDINGS.md — независимый adversarial-аудит playerok-control-center

HEAD: dcb728305e0448066e73815b9fab15bd9b4ad8fc · 2026-11-01 · read-only (production-код не менялся, live-операций нет).

Критичность: P0 (деньги/данные/полная неработоспособность) → P3 (косметика).
Статусы: CONFIRMED = доказано кодом и/или исполняемым RED-тестом; PROBABLE = доказано чтением кода, живого репро нет; REJECTED = проверено, не дефект.

---

## P0 — объясняют оба пользовательских симптома («половина кнопок не реагирует», «/start иногда не восстанавливает меню»)

### G1. ACK-гейт дропает ~59% кнопок при любом >2s ответе Telegram и ЗАТИРАЕТ текущий экран — CONFIRMED (parent-verified)
- **Файлы:** `app/telegram/callback_ack.py:18-28` (2s ACK_TIMEOUT; TelegramNetworkError/Timeout → status='transport'), `app/telegram/router.py:139-147`.
- **Масштаб (AST-подсчёт):** 600 интерактивных кнопок в app/telegram; **356 (59.3%)** эмитят callback вне navigation() allowlist; по литералам — 212/429 = 49.4%. Гейтируются: весь мастер создания (w:×42), пагинация чатов/списков, все подтверждения, фильтры, ops/aq/ar-панели, commit:, quick:, gft:-поддействия (14), reply:, readchat:.
- **Сценарий:** прокси/Worker RTT > 2s → `query.answer` таймаут → статус != 'ok' → действие НЕ выполняется, а экран ЗАМЕНЯЕТСЯ предупреждением + главное меню (render через enter("input")) → пользователь теряет позицию в мастере/списке. Read-only маршруты гейтируются непоследовательно (orderpage:/listnext:/chatpage:/il: гейтируются, stats:/chat: — нет).
- **Ожидается:** отклонение без разрушения экрана; **фактически:** дроп + замена экрана.

### G2. Кнопки «Ответить»/«Прочитано» на уведомлениях о сообщениях мертвы ВСЕГДА — CONFIRMED (parent-verified скриптом)
- `message_notice.py:23-28` шлёт reply:/readchat:/chat:; `navigation.py:265-266`: при несовпадении panel.id (уведомления никогда не усыновляются панелью, navigation.py:343-344) допускается ТОЛЬКО safe_route или compose:/ff:start: — `reply:` и `readchat:` там отсутствуют (проверено grep+python) → `allowed()`=False → alert «Экран закрыт или устарел». «📖 Диалог» (chat:) работает. Это повседневные кнопки владельца магазина.

### G3. Один сетевой сбой при отправке экрана замораживает панель НАВСЕГДА, включая «🏠 Главное меню», без feedback — CONFIRMED
- `navigation.py:383` sending=True до send; сброс только на успех (395/410); TelegramNetworkError проглатывается `callback_ack.py:114-119` → sending остаётся True → каждый последующий render() абортируется (navigation.py:305-311) и None молча проглатывается вызывающими (router.py:54-66 display, 616/737). Панель «зависает» — работает только /start→recovery или /panel_reset.
- **Ожидается:** видимое сообщение об ошибке/восстановление; **фактически:** тишина.

### G4. /start молча умирает на неэдитируемом старом сообщении — CONFIRMED (код-путь)
- `navigation.py:381-382`: `edit_message_reply_markup` ВНЕ try/except; при неэдитируемой старой панели (окно редактирования Telegram истекло) BadRequest выходит из render() → /start (router.py:98-107, без catch) → dispatcher логирует, пользователь не видит ничего. Состояние панели персистентно (settings) — переживает рестарты. Объясняет «/start иногда не восстанавливает»: спасает только неочевидный /panel_reset (router.py:119-122, сбрасывает id до render).
- Доп. пути: незащищённый retry-эдит в recovery_prompt (navigation.py:199-206) и re-raise при BadRequest вне whitelist (207-211).

### G5. ACTION_MAX_WAIT замеряется ДО per-user FSM-лока — медленный обработчик дропает очередь нажатий — CONFIRMED
- `install_callback_ack` (callback_ack.py:122-127) регистрирует middleware ПЕРЕД dispatcher.fsm → checked_at штампуется до ожидания FSM-лока. Обработчик, держащий лок >10s (router.py:521 sync, gifts_panel.py:178 30s-timeout), делает следующий закеуедный клик «слишком старым», хотя ack был ok. Дроп идёт по пути G1 (с разрушением экрана).

### G6. Отказ-алерт дважды отвечает на уже отвеченный callback — PROBABLE
- `router.py:138` `query.answer(...)` после middleware-ответа (callback_ack.py:111) → TelegramBadRequest «query is too old/already answered» вне catch-листа router.py:617 → алерт невидим, отказы G2/G3 выглядят как «ноль реакции». Совместим с жалобой «кнопки не реагируют».

## P1 — подтверждённые дефекты

### F1. Системные события чата Playerok превращаются в бессмысленные уведомления «Неизвестный отправитель» — CONFIRMED
- **Файлы:** `app/services/orders.py:222-236` (ingest без фильтра событий), `app/telegram/message_notice.py:7-15`, `app/workers/notifications.py:54-66`, триггер `app/workers/realtime.py:103-107` → `app/workers/sync.py:62-68,168-174`.
- **Первопричина:** Playerok шлёт в чат системные шаблоны (`{{ITEM_PAID}}`, `{{ITEM_SENT}}`, `{{DEAL_CONFIRMED}}`, `{{DEAL_ROLLED_BACK}}`, `{{DEAL_HAS_PROBLEM}}`, `{{DEAL_PROBLEM_RESOLVED}}`) сообщениями с `user=None` (см. `playerokapi/listener/listener.py:167-235` — там это распознаётся как deal-events). Приложение проглатывает их как обычные «сообщения покупателя»: ingest_message пишет kind='message' в inbox, Notifications.once рендерит `👤 Неизвестный отправитель` + сырой `{{ITEM_PAID}}`, рядом с нормальным уведомлением «Заказ … PAID» (kind='order'). Пропуск только если author.id == account_id (notifications.py:55-57) — user=None не совпадает.
- **Воспроизведение:** `tests/test_audit_red_repro.py::test_system_item_paid_event_is_not_a_user_message` и `::test_message_notice_renders_placeholder_as_unknown_sender` — оба FAIL (RED). Реальный триггер: WS `chatUpdated` на чат, где lastMessage = {{ITEM_PAID}} (типично сразу после оплаты) → sync.request('chat') → скан сообщений → уведомление.
- **Ожидается:** системные события фильтруются/маппятся на статусы сделки; **фактически:** дублирующий шум «от неизвестного».
- **Компоненты:** sync, notifications, chat UI. **Регрессия при исправлении:** при фильтрации не потерять genuine-события {{DEAL_HAS_PROBLEM}} — их показ обязателен (см. order_problem-интеграцию).

### F2. Кнопка «Открыть результат» после ответа/прочитано всегда даёт «Результат операции недоступен» — CONFIRMED
- **Файлы:** `app/database/store.py:230-250` (receipts только для create/publish/edit/edit_fields/discount/delete/delivery/delivery_text/fulfill/gift_send/gift_refund/gift_notice/premium_publish/premium_bump — БЕЗ reply/mark_read/chain_message), `app/telegram/router.py:446-451` (обещает результат для ЛЮБОГО kind), `app/telegram/operation_results.py:44-48` (JOIN с operation_receipts → пусто → Conflict).
- **Воспроизведение:** `tests/test_audit_red_repro.py::test_result_show_after_reply_is_available`, `::test_result_show_after_mark_read_is_available` — FAIL с `Conflict: Результат операции недоступен`.
- **Ожидается:** экран статуса операции; **фактически:** конфликт на каждое нажатие. UI обещает «Подтверждённый результат придёт отдельным сообщением» (router.py:450) — для reply/mark_read оно не приходит никогда (см. F3/F4).
- **Регрессия при исправлении:** добавление receipts для reply/mark_read включит их в notify()-рассылку — проверить двойные уведомления с refresh_actions.

### F3. Ответы из панели чата (compose:/reply:) не обновляют исходное уведомление и не создают receipt — CONFIRMED (subagent N4, parent-verified)
- **Файлы:** `app/telegram/router.py:506,518` — `origin = replies.replies.origin(owner, message.message_id, ident)`; `app/services/replies.py:78-83` — origin ищется по `notifications.telegram_message_id`, но сообщения панели никогда не являются уведомлениями (`app/telegram/navigation.py:343-344` «Proofs and notifications are never adopted as panels») → origin=None; `app/services/replies.py:102-106` — notification_actions создаётся только при origin; `app/workers/notifications.py:226-231` — refresh_actions видит только события с notification_actions.
- **Итог:** ответ, отправленный из экрана «Переписка», заканчивается молча (статус виден только в ops:list). Тот же ответ, отправленный кнопками самого уведомления, корректно обновляет уведомление. Несимметрично и нарушает обещание UI.
- **Регрессия при исправлении:** связывать origin по chat_id + последнему уведомлению, аккуратно с несколькими окнами.

### F4. operation_receipts.notice_status=UNCERTAIN терминален — квитанция о результате молча теряется — CONFIRMED (subagent N6, parent-verified)
- **Файлы:** `app/telegram/operation_results.py:187-215` (PENDING→PROCESSING→SENT/UNCERTAIN; при сбое Telegram → UNCERTAIN), `app/workers/notifications.py:311` (на старте только PROCESSING→UNCERTAIN). Ни одного пути UNCERTAIN→PENDING во всём app/ (grep подтверждён лично).
- **Итог:** единственный сбой send_message при доставке квитанции — и владелец никогда не узнает о результате операции (обещание «придёт отдельным сообщением» нарушено), кроме случая, когда он сам зайдёт в result:list.
- **Регрессия при исправлении:** повторная отправка безопасна (новое сообщение, не редактирование) — нужен counts/bounded retry с CAS на notice_status.

### F5. mark_read из чата не даёт никакой обратной связи — CONFIRMED (subagent N5, parent-verified)
- Воспроизводит F2+F3 для kind='mark_read': кнопка «✅ Прочитано» в chat_panel (chat_panel.py:75) → origin=None → ни receipt, ни обновления уведомления. Статус операции проверяется только вручную.

### F6. Панель чата показывает сырой шаблон `{{ITEM_PAID}}` как текст сообщения — CONFIRMED (subagent N2)
- `app/telegram/chat_panel.py:69-71`: автор корректно «Событие», но `text = message.get("text") or "[событие]"` — `{{ITEM_PAID}}` truthy → показывается сырой плейсхолдер.

## P1 — подтверждённые дефекты (продолжение: API-слой и автовыдача)

### F10. Преходящий сетевой сбой на preflight-чтении терминирует операцию и навсегда карантинит зарезервированный товар — CONFIRMED (subagent, repro исполнен; parent-verified)
- **Файлы:** `app/playerok/adapter.py:172-197` (`uncertain=not read` — для мутаций ANY network/HTTP_5xx/429/NON_JSON/GRAPHQL_ERROR = certain=PERMANENT), `app/workers/outbox.py:661-667`, `app/database/store.py:383-390` (`DELIVERED if SUCCEEDED else QUARANTINED` — без различия PERMANENT_FAILED vs UNCERTAIN).
- **Сценарий:** delivery op → `get_deal` (preflight) ловит 503/429/timeout → APIError(uncertain=False) → PERMANENT_FAILED → inventory QUARANTINED. Единица потеряна: QUARANTINED→AVAILABLE перехода не существует (grep: только writers quarantine/deliver), reservations не удаляются, `Reconciliation.action` (reconciliation.py:43-47) принимает только UNCERTAIN/MANUALLY_STOPPED_UNCERTAIN → ручной путь разрешения отсутствует. Оплаченный заказ недоставаем, секрет «уничтожен».
- **Доп. подтверждение:** статус `RETRYABLE_FAILED` («Ожидает безопасной обработки» в UI) не пишется ни одним кодом — designed recovery state недостижим.
- **Ожидается:** провал preflight-чтения (запрос НЕ отправлялся) → повторяемость/блокировка до реконнекта; **фактически:** терминальная потеря резерва.

### F11. reserve() не проверяет problem-аннотацию заказа — CONFIRMED (subagent, repro исполнен)
- `app/services/orders.py:169-198` резервирует/ставит delivery-op для заказа с problem=1 (в отличие от executor outbox.py:123-126 и chains delivery_chains.py:63-66). Op неизбежно падает на dispatch → PERMANENT_FAILED → F10 уничтожает единицу. Асимметрия проверки гарантирует money-loss при problem, выставленном между reserve и send.

### F12. Ручной reserve на non-stock (fixed_text) лоте выключает его авто-выдачу — CONFIRMED (subagent, repro исполнен)
- `orders.py:151-155,196-198`: без inventory → empty_stock ставит `delivery:{item}:enabled=false` даже для текстового товара; enrollment для будущих заказов блокируется. Восстановление — только полный re-save политики.

### F13. Карантин при PERMANENT_FAILED внутри delivery-цепочки — CONFIRMED
- `app/services/delivery_chains.py:140-149`: `failed = any(UNCERTAIN, PERMANENT_FAILED)` → QUARANTINED без различия статуса; доказуемо-неотправленный шаг (ValidationError при STOP политики) уничтожает секрет. Тот же класс, что F10.

### F14. 4xx/429 на мутациях классифицируются UNCERTAIN, хотя 4xx доказывает «не отправлено» — CONFIRMED
- `adapter.py:181-191`: UNAUTHORIZED/RATE_LIMITED/HTTP_4xx для мутаций → uncertain=True → для delivery QUARANTINE + «запрос мог выполниться, не повторяйте». Инверсия F10-класса: пере-консервативность, создающая ручную нагрузку без необходимости. Кодовая база сама считает 4xx releasable (store.py:296-299).

### F15. `Playerok.pages()` не ограничен по числу страниц — CONFIRMED
- `adapter.py:489-498`: while True без капа (PageScanner имеет pages_per_stream=4 и PAGINATION_PASS_LIMIT=10000). Endless-cursor (fresh cursor каждый раз) → бесконечный цикл; вызывается из Products.schema_details / Agreements.pending / gifts/service.

### F16. edit_fields принимает результат мутации по одному id — CONFIRMED
- `outbox.py:364-376`: проверка `result.get("id")==target` без полевой сверки (в отличие от edit/discount/create с edit_receipt_matches). Сервер может молча отбросить attributes/dataFields → SUCCEEDED + draft UPDATED. Нарушает собственный стандарт репо (item_receipts.py:5).

### F17. Enum-alias CONFIRMATION→RESTRICTION — CONFIRMED (исполнено)
- `playerokapi/enums.py:279-284`: CONFIRMATION = 0 (alias) → `.name` даёт 'RESTRICTION'; иконки подтверждения показываются как ограничения. ItemStatuses/ItemDealDirections/ItemDealStatuses roundtrip — чисто (проверено).

## P2 — подтверждённые дефекты API-слоя

### F18. Инвалидация сессии во время полёта → успешные чтения отбрасываются, op терминируется — CONFIRMED
- `adapter.py:459-464`: post-call проверка `connection.state != CONNECTED` (нарушена конкур. инвалидацией) → APIError certain → PERMANENT + карантин. Mid-POST: adapter.py:157-162 корректно ловит до отправки, но всё равно терминально.


### F7. Payload сообщения в inbox заморожен первым ingest — правки/удаления не отражаются — CONFIRMED (subagent N7)
- `app/services/orders.py:225-235`: inbox key `message:{id}` INSERT OR IGNORE — обновляется только chat_messages; рендер уведомлений читает inbox payload (notifications.py:53,264) → удалённое сообщение навсегда показывает старый текст; ветка «[сообщение удалено]» достижима только если сообщение было удалено ДО первого ingest. Также нет фильтра is_read: сообщение, пришедшее уже прочитанным (chatMarkedAsRead-скан), всё равно триггерит уведомление.

### F8. defer_update портит счётчики отказов всех операций события — CONFIRMED (subagent N8)
- `app/workers/notifications.py:300-306`: `UPDATE notification_actions SET retry_after=?,failures=? WHERE event_key=? AND owner_id=?` — без фильтра operation_id: один неудачный edit ставит backoff всем операциям события; `max(failures)+1` раздувает счётчик через чужие операции. Книга учёта/backoff искажается (данных не теряет).

## P3 / наблюдения

### F9. (PROBABLE) Уведомление «Заказ» не отражает уже изменившийся статус
- `app/workers/notifications.py:68-74`: рендер по payload события, но перед отправкой сверяет актуальный статус заказа и SKIP при изменении — ок; однако статус в тексте остаётся payload-ным. Мелочь.

## Проверенные инварианты (money-critical, не сломаны)

- **Gifts exactly-once:** `app/gifts/service.py:589-633` — SPENDING/spent_at/form_id/резерв Stars фиксируются в одной BEGIN IMMEDIATE транзакции ДО `sender.pay()` (строка 648); любое исключение до записи оставляет step CHECKING без spent_at → без повтора. `pay()` (`app/gifts/mtproto.py:230-249`) — единственный вызов SendStarsFormRequest, форма готовится с уникальным reference (prepare, строка 562-567), PAYMENT_FORM_ALREADY_USED-защита (строка 624-625), DELIVERY_RECEIPT_ALREADY_USED (653-666).
- **Unknown-step admission:** `retry_preflight` (service.py:789-831) требует state='BLOCKED' + `spent_at IS NULL` + `form_id IS NULL` + op in PERMANENT_FAILED/UNCERTAIN + bundle/description hash совпадают; `reconcile` (766-787) продвигает ровно один UNCERTAIN-step по CAS с реальным receipt-ом. UNCERTAIN не переплачивается.
- **Дневной бюджет:** service.py:606-623 — день UTC ISO; `actual` = SUM(steps spent_at>=today) + `reservations` = SUM(gift_jobs.reserved_stars) глобально (консервативно, включая старые дни) + remaining. Двойного счёта нет (reserved обнуляется при SUCCEEDED, строка 687).
- **Диспетч-фенс:** generation-счётчик spent_at шагов сверяется внутри транзакции (574→595-599) — гонка с concurrent preflight ловится BALANCE_OBSERVATION_STALE.
- **gift_refund после gift_send:** guard требует `job.state='INVALID_RECIPIENT' AND spent_at IS NULL AND form_id IS NULL AND refund_state='QUEUED'` (execution.py:96-117, service.py:752) — после успешной отправки возврат невозможен; CAS refund_state QUEUED→SPENDING (execution.py:110-117).
- **Доставка секрета:** outbox.py:599-624 перепроверяет get_deal (PAID/без проблемы/владелец/чат) + резерв перед отправкой секрета; пустой `result['id']` → UNCERTAIN (DELIVERY_RESULT_UNKNOWN).
- **Enqueue-изоляция:** store.py:149-230 — повторная регистрация выдачи/fulfill блокируется по статусам PENDING/PROCESSING/RETRYABLE/UNCERTAIN; gift/chain взаимоисключение; конфликтные item-операции блокируются.
- **Автовыдача изorders.py reserve:** резерв AVAILABLE→RESERVED + reservations + outbox в одной транзакции; повторный reserve возвращает существующий inventory_id.

## False-green тесты (аудит стенда)

- **T1 (P1):** кнопка result:show после reply/mark_read не покрыта НИ ОДНИМ тестом (test_telegram_integration.py:249-296 делает commit→worker→edit, но не нажимает собственную кнопку; test_operation_results_ui.py:92 жмёт её только для receipt-covered kinds) → F2 зелёный по недосмотру.
- **T2 (P1):** фейки сообщений всегда с user+username; author-line никогда не ассертится → F1 незаметен (CONFIRMED-RED repro).
- **T3 (P2):** ветка `message is not modified` в refresh_actions (notifications.py:275) — нулевое покрытие; ошибочная классификация уведёт в вечный re-edit + backoff-стагнацию.
- **T4 (P2):** ни один тест не комбинирует сбой ACK с мутирующим commit: (gate router.py:139-146 не проверен для buyer-facing flow).
- **T5 (P2):** rule-F нарушение: reply-тесты вручную финишируют outbox (test_replies.py:42-44) вместо запуска executor.
- **T6 (P2):** ручные UPDATE operation_receipts ... SET notice_status='SENT' (test_functional_audit.py:175-178, test_interface_cleanup.py:72-74) обходят writer — регрессия notify() осталась бы зелёной.
- **T7 (P3):** ACTION_MAX_WAIT (10s age) ветка мертва в тестах; колбэки всегда свежие.
- **T8 (P3):** backoff-расписание defer_update подменено ручным `retry_after=0` (test_replies.py:107).

## Деньги/статусы — сводка рисков

- **P0 двойного списания/двойной выдачи не найдено** — gifts exactly-once проверен лично по коду (SPENDING/spent_at/form_id до pay(); PAYMENT_FORM_ALREADY_USED; reconcile только с receipt-ом; refund требует spent_at IS NULL). Изоляция enqueue/claim/finish — с CAS.
- **Главный реальный риск — liveness денег:** F10/F11/F13 (терминальная потеря резерва при преходящих сбоях) — единица карантинится без пути возврата; F14 — обратная сторона (лишний ручной UNCERTAIN).
- UX-обещания vs поведение: F1-F8.


## План исправления по приоритетам

1. **G1-G6 (оба симптома пользователя, один класс):** (a) расширить navigation() allowlist до всех read-only маршрутов (orderpage:, listnext:, chatpage:, itemfilter:, il:, sd:, sg:, st:, ops:, aq:, ar:, gft:-поддействия) — гейт должен касаться ТОЛЬКО мутаций; (b) дроп-путь НЕ должен рендерить новый экран (просто alert); (c) `reply:`/`readchat:` добавить в допустимые при panel.id-несовпадении (это уведомления, не панели) — либо перенести эти кнопки в safe_route; (d) sending=True сбрасывать в except-ветке render(); (e) обернуть edit_message_reply_markup (navigation.py:381-382) и retry-эдиты recovery_prompt; (f) не отвечать на query дважды (убрать query.answer в router.py:138 или отвечать там единожды).
2. **F10+F11+F13 (money-liveness):** в `Store.finish`/chain-finish различать PERMANENT_FAILED без внешней отправки → возвращать единицу AVAILABLE вместо QUARANTINED; problem-проверка в reserve(); путь QUARANTINED→AVAILABLE через Reconciliation для доказуемо-неотправленных. RED-тест: reserve → finish(PERMANENT_FAILED) → assert AVAILABLE.
3. **F14:** классифицировать HTTP 4xx/429 на мутациях как не-UNCERTAIN (repo уже считает их releasable, store.py:296-299).
4. **F1+F6:** фильтр системных шаблонов в ingest_message + человекочитаемые метки. RED-тесты уже в tests/test_audit_red_repro.py.
5. **F2+F3+F5:** включить reply/mark_read в receipts-набор или убрать обещание/кнопку.
6. **F4:** bounded retry notice_status UNCERTAIN→PENDING (CAS, счётчик).
7. **F15:** кап страниц в pages(). **F16:** полевая сверка edit_fields. **F7/F8/F17/F18:** по мере.
