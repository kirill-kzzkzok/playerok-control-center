# UX redesign — inventory and navigation

Baseline: c8ffed3. Этапы A–E завершены. Финальные проверки этапа E зафиксированы ниже. Backend, .env, рабочая SQLite и сессии не изменяются.

Direction: five top-level entries, compact summary → contextual actions → details. Unknown results stay explicit.

## Full route and FSM inventory (source generated, reviewed alongside panels)

### agreements_panel.py

| Existing function / route | New destination / access | Checks retained |
|---|---|---|

FSM: `state.set_state(None)`


Buttons (source inventory; callbacks remain compatible):

- `('К диагностике', 'diagnostics')`
- `('Далее', f'agv:{parts[1]}:page:{index + 1}')`
- `('Назад', f'agv:{parts[1]}:page:{index - 1}')`
- `('Далее', f'agv:list:{page + 1}')`
- `('Назад', f'agv:list:{page - 1}')`

### auto_replies_panel.py

| Existing function / route | New destination / access | Checks retained |
|---|---|---|
| `action.startswith('ar:history:')` | Ещё → Настройки → Автоответы → правило / журнал | Original owner, nonce, FSM, CAS, service guards |
| `action == 'ar:menu'` | Ещё → Настройки → Автоответы → правило / журнал | Original owner, nonce, FSM, CAS, service guards |
| `action == 'ar:stop'` | Ещё → Настройки → Автоответы → правило / журнал | Original owner, nonce, FSM, CAS, service guards |
| `command == 'commit'` | Ещё → Настройки → Автоответы → правило / журнал | Original owner, nonce, FSM, CAS, service guards |
| `command == 'show'` | Ещё → Настройки → Автоответы → правило / журнал | Original owner, nonce, FSM, CAS, service guards |
| `command == 'toggle'` | Ещё → Настройки → Автоответы → правило / журнал | Original owner, nonce, FSM, CAS, service guards |
| `command == 'delete'` | Ещё → Настройки → Автоответы → правило / журнал | Original owner, nonce, FSM, CAS, service guards |
| `current in steps` | Ещё → Настройки → Автоответы → правило / журнал | Original owner, nonce, FSM, CAS, service guards |
| `action == 'ar:new'` | Ещё → Настройки → Автоответы → правило / журнал | Original owner, nonce, FSM, CAS, service guards |
| `action.startswith('ar:edit:')` | Ещё → Настройки → Автоответы → правило / журнал | Original owner, nonce, FSM, CAS, service guards |
| `current != 'autoreply:limit'` | Ещё → Настройки → Автоответы → правило / журнал | Original owner, nonce, FSM, CAS, service guards |

FSM: `state.set_state('autoreply:confirm')`, `state.set_state('autoreply:trigger')`, `state.set_state(following)`


Buttons (source inventory; callbacks remain compatible):

- `('Новое правило', 'ar:new')`
- `('Выключить все', 'ar:stop')`
- `('Журнал обработки', 'ar:history:0')`
- `('Ручные шаблоны', 'quickreplies')`
- `('Настройки', 'settings')`
- `('Редактировать (с выключением)', 'ar:edit:' + ident)`
- `('Список правил', 'ar:menu')`
- `('Удалить правило', 'ar:delete:' + ident)`
- `('← Назад', f'ar:history:{page - 1}')`
- `('Далее →', f'ar:history:{page + 1}')`
- `('Правила', 'ar:menu')`
- `('Подтвердить локально', 'ar:commit:' + nonce)`
- `('Отмена', 'ar:menu')`

### automation_panel.py

| Existing function / route | New destination / access | Checks retained |
|---|---|---|
| `action == 'automation'` | Ещё → Автовыдача → товар → параметры / дополнительные параметры | Original owner, nonce, FSM, CAS, service guards |
| `action == 'auto:stop'` | Ещё → Автовыдача → товар → параметры / дополнительные параметры | Original owner, nonce, FSM, CAS, service guards |
| `command == 'mode'` | Ещё → Автовыдача → товар → параметры / дополнительные параметры | Original owner, nonce, FSM, CAS, service guards |
| `command == 'toggle'` | Ещё → Автовыдача → товар → параметры / дополнительные параметры | Original owner, nonce, FSM, CAS, service guards |
| `command in {'edit', 'clear'}` | Ещё → Автовыдача → товар → параметры / дополнительные параметры | Original owner, nonce, FSM, CAS, service guards |
| `command == 'clear'` | Ещё → Автовыдача → товар → параметры / дополнительные параметры | Original owner, nonce, FSM, CAS, service guards |
| `command == 'save'` | Ещё → Автовыдача → товар → параметры / дополнительные параметры | Original owner, nonce, FSM, CAS, service guards |
| `command == 'arm'` | Ещё → Автовыдача → товар → параметры / дополнительные параметры | Original owner, nonce, FSM, CAS, service guards |
| `command == 'orders'` | Ещё → Автовыдача → товар → параметры / дополнительные параметры | Original owner, nonce, FSM, CAS, service guards |
| `command == 'simulate'` | Ещё → Автовыдача → товар → параметры / дополнительные параметры | Original owner, nonce, FSM, CAS, service guards |
| `command == 'plan'` | Ещё → Автовыдача → товар → параметры / дополнительные параметры | Original owner, nonce, FSM, CAS, service guards |
| `command == 'start_chain'` | Ещё → Автовыдача → товар → параметры / дополнительные параметры | Original owner, nonce, FSM, CAS, service guards |

FSM: `state.set_state(None)`, `state.set_state('automation:text')`


Buttons (source inventory; callbacks remain compatible):

- `('Ручной', f'ap:{nonce}:mode:manual')`
- `('Текст', f'ap:{nonce}:mode:text')`
- `('Запас', f'ap:{nonce}:mode:stock')`
- `('Переключить желаемое включение', f'ap:{nonce}:toggle:enabled')`
- `('📦 Выбрать товар', 'items')`
- `('🚀 Продвижение · dry-run', 'promotions')`
- `('Все политики выдачи', 'aq:list:policies:0')`
- `('Цепочки выдачи', 'aq:list:chains:0')`
- `('Регистрация будущих заказов', 'aq:list:enrollment:0')`
- `('⛔ Выключить все политики выдачи', 'auto:stop')`
- `('Очистить', f'ap:{nonce}:clear:{field}')`
- `('Ожидать подтверждения владельца после выдачи', f'ap:{nonce}:toggle:mark_sent')`
- `('📚 Склад', 'stock:view:' + data['id'])`
- `('Будущие заказы автоматически', f'ap:{nonce}:toggle:auto_enroll')`
- `('💾 Сохранить политику', f'ap:{nonce}:save')`
- `('🔎 Dry-run оплаченных заказов', f'ap:{nonce}:orders')`
- `('Автоматизация', 'automation')`
- `('✅ Сохранить и включить будущие продажи', f"ap:{data['nonce']}:arm")`
- `('К политике', 'auto:item:' + data['id'])`
- `('✅ Запустить цепочку', f"ap:{data['nonce']}:start_chain")`
- `('Подготовить запуск цепочки', f"ap:{data['nonce']}:plan")`
- `('Заказ', 'order:' + data['selected_order'])`

### automation_queue.py

| Existing function / route | New destination / access | Checks retained |
|---|---|---|
| `command == 'evaluation'` | Ещё → Инструменты → Автовыдача / продвижение → журналы | Original owner, nonce, FSM, CAS, service guards |
| `command == 'target'` | Ещё → Инструменты → Автовыдача / продвижение → журналы | Original owner, nonce, FSM, CAS, service guards |
| `command == 'recheck'` | Ещё → Инструменты → Автовыдача / продвижение → журналы | Original owner, nonce, FSM, CAS, service guards |
| `command in {'chain', 'stop'}` | Ещё → Инструменты → Автовыдача / продвижение → журналы | Original owner, nonce, FSM, CAS, service guards |
| `command == 'apply'` | Ещё → Инструменты → Автовыдача / продвижение → журналы | Original owner, nonce, FSM, CAS, service guards |
| `command == 'stop'` | Ещё → Инструменты → Автовыдача / продвижение → журналы | Original owner, nonce, FSM, CAS, service guards |

FSM: `state.set_state(None)`


Buttons (source inventory; callbacks remain compatible):

- `('← Назад', f'aq:list:{name}:{page - 1}')`
- `('Далее →', f'aq:list:{name}:{page + 1}')`
- `('Автоматизация', 'automation')`
- `('Продвижение', 'promotions')`
- `('⛔ Остановить цепочку', 'aq:stop:' + ident)`
- `('Проверить выполнение', 'ff:start:' + ident)`
- `('Заказ', 'order:' + ident)`
- `('Обновить', 'aq:chain:' + ident)`
- `('Цепочки', 'aq:list:chains:0')`
- `('Товар', 'promo:item:' + row['item_id'])`
- `('История', 'aq:list:evaluations:0')`
- `('Настроить выбор/проверить', 'promo:item:' + ident)`
- `('Повторить по расписанию', 'aq:recheck:' + ident)`
- `('Очередь', 'aq:list:schedule:0')`
- `('Цель', 'aq:target:' + ident)`
- `('Цепочка', 'aq:chain:' + data['id'])`
- `('⛔ Остановить', 'aq:apply:' + nonce)`

### callback_ack.py

| Existing function / route | New destination / access | Checks retained |
|---|---|---|
| `action in {'home', 'profile', 'profile:check', 'diagnostics', 'settings', 'items', 'chats', 'orders', 'create', 'drafts', 'automation', 'promotions', 'quickreplies', 'gft:home', 'gft:stop', 'gft:catalog', 'gft:bindings'}` | Ещё → Инструменты → Автовыдача / продвижение → журналы | Original owner, nonce, FSM, CAS, service guards |
| `action.startswith(('item:', 'order:', 'chat:', 'stats:', 'result:list:', 'result:show:', 'delivered:', 'gft:job:', 'gft:list:', 'gft:item:', 'stock:list:'))` | Ещё → Инструменты → Автовыдача / продвижение → журналы | Original owner, nonce, FSM, CAS, service guards |

Buttons (source inventory; callbacks remain compatible):

- `('query is too old', 'query id is invalid')`

### chat_panel.py

| Existing function / route | New destination / access | Checks retained |
|---|---|---|
| `action.startswith('chat:')` | Сообщения → диалог → Ответить / Быстрый ответ / Прочитано / Ещё | Original owner, nonce, FSM, CAS, service guards |

Buttons (source inventory; callbacks remain compatible):

- `('✉️ Ответить', 'reply:' + ident)`
- `('✅ Прочитано', 'readchat:' + ident)`
- `('← Новее', f"chatpage:{view['nonce']}:newer")`
- `('Старее →', f"chatpage:{view['nonce']}:older")`
- `('🔄 Последние', 'chat:' + ident)`
- `('← Фото', f"chatmedia:{view['nonce']}:{index - 1}")`
- `('Фото →', f"chatmedia:{view['nonce']}:{index + 1}")`
- `('📷 Открыть фотографии', f"chatmedia:{view['nonce']}:0")`
- `('💬 Диалог', 'chat:' + view['id'])`

### drafts_panel.py

| Existing function / route | New destination / access | Checks retained |
|---|---|---|
| `action == 'drafts'` | Товары → Черновики → продолжить сохранённый мастер | Original owner, nonce, FSM, CAS, service guards |
| `action == 'dl:filters'` | Товары → Черновики → продолжить сохранённый мастер | Original owner, nonce, FSM, CAS, service guards |
| `action.startswith('dl:filter:')` | Товары → Черновики → продолжить сохранённый мастер | Original owner, nonce, FSM, CAS, service guards |
| `action.startswith('dl:page:')` | Товары → Черновики → продолжить сохранённый мастер | Original owner, nonce, FSM, CAS, service guards |
| `action == 'dl:search'` | Товары → Черновики → продолжить сохранённый мастер | Original owner, nonce, FSM, CAS, service guards |

FSM: `state.set_state(None)`, `state.set_state('draftlist:search')`


Buttons (source inventory; callbacks remain compatible):

- `('← Назад', f"dl:page:{result['page'] - 1}:{selected}")`
- `('Далее →', f"dl:page:{result['page'] + 1}:{selected}")`
- `('🔎 Поиск', 'dl:search')`
- `('Статусы', 'dl:filters')`
- `('Сбросить поиск и фильтр', 'drafts')`
- `('➕ Новый черновик', 'create')`
- `('Отмена поиска', 'dl:page:0:' + selected)`

### finance_panel.py

| Existing function / route | New destination / access | Checks retained |
|---|---|---|
| `action == 'providers'` | Товар → Другие действия → Финансовый расчёт; Настройки → Финансовые параметры | Original owner, nonce, FSM, CAS, service guards |
| `action.startswith('fin:select:')` | Товар → Другие действия → Финансовый расчёт; Настройки → Финансовые параметры | Original owner, nonce, FSM, CAS, service guards |
| `action == 'fin:refresh'` | Товар → Другие действия → Финансовый расчёт; Настройки → Финансовые параметры | Original owner, nonce, FSM, CAS, service guards |
| `action.startswith('finance:')` | Товар → Другие действия → Финансовый расчёт; Настройки → Финансовые параметры | Original owner, nonce, FSM, CAS, service guards |
| `action.startswith('fin:cost:')` | Товар → Другие действия → Финансовый расчёт; Настройки → Финансовые параметры | Original owner, nonce, FSM, CAS, service guards |
| `action.startswith('fin:placement:')` | Товар → Другие действия → Финансовый расчёт; Настройки → Финансовые параметры | Original owner, nonce, FSM, CAS, service guards |
| `action.startswith('fin:place:')` | Товар → Другие действия → Финансовый расчёт; Настройки → Финансовые параметры | Original owner, nonce, FSM, CAS, service guards |
| `action.startswith('fin:clearcost:')` | Товар → Другие действия → Финансовый расчёт; Настройки → Финансовые параметры | Original owner, nonce, FSM, CAS, service guards |
| `current == 'finance:amount'` | Товар → Другие действия → Финансовый расчёт; Настройки → Финансовые параметры | Original owner, nonce, FSM, CAS, service guards |
| `current == 'finance:cost'` | Товар → Другие действия → Финансовый расчёт; Настройки → Финансовые параметры | Original owner, nonce, FSM, CAS, service guards |
| `action.startswith(('fin:', 'finance:'))` | Товар → Другие действия → Финансовый расчёт; Настройки → Финансовые параметры | Original owner, nonce, FSM, CAS, service guards |

FSM: `state.set_state(None)`, `state.set_state('finance:amount')`, `state.set_state('finance:cost')`


Buttons (source inventory; callbacks remain compatible):

- `('fin:', 'finance:')`
- `('💳 Способ вывода', 'providers')`
- `('⭐ Стоимость нового размещения', 'fin:placement:' + ident)`
- `('✏️ Себестоимость', 'fin:cost:' + ident)`
- `('Очистить себестоимость', 'fin:clearcost:' + ident)`
- `('🔄 Обновить расчёт', 'finance:' + ident)`
- `('⬅️ Товар', 'item:' + ident)`
- `('🔄 Обновить условия', 'fin:refresh')`
- `('Сменить способ', 'providers')`

### fulfillment_panel.py

| Existing function / route | New destination / access | Checks retained |
|---|---|---|
| `step == 'check'` | Заказ → Подтвердить выполнение (существующая проверка передачи и отчёт) | Original owner, nonce, FSM, CAS, service guards |
| `step in PROMPTS` | Заказ → Подтвердить выполнение (существующая проверка передачи и отчёт) | Original owner, nonce, FSM, CAS, service guards |
| `step == 'provider'` | Заказ → Подтвердить выполнение (существующая проверка передачи и отчёт) | Original owner, nonce, FSM, CAS, service guards |
| `step == 'currency'` | Заказ → Подтвердить выполнение (существующая проверка передачи и отчёт) | Original owner, nonce, FSM, CAS, service guards |
| `step not in PROMPTS` | Заказ → Подтвердить выполнение (существующая проверка передачи и отчёт) | Original owner, nonce, FSM, CAS, service guards |
| `command == 'check'` | Заказ → Подтвердить выполнение (существующая проверка передачи и отчёт) | Original owner, nonce, FSM, CAS, service guards |
| `command == 'continue'` | Заказ → Подтвердить выполнение (существующая проверка передачи и отчёт) | Original owner, nonce, FSM, CAS, service guards |
| `command == 'refresh'` | Заказ → Подтвердить выполнение (существующая проверка передачи и отчёт) | Original owner, nonce, FSM, CAS, service guards |
| `command == 'page'` | Заказ → Подтвердить выполнение (существующая проверка передачи и отчёт) | Original owner, nonce, FSM, CAS, service guards |
| `command == 'provider'` | Заказ → Подтвердить выполнение (существующая проверка передачи и отчёт) | Original owner, nonce, FSM, CAS, service guards |
| `command == 'currency'` | Заказ → Подтвердить выполнение (существующая проверка передачи и отчёт) | Original owner, nonce, FSM, CAS, service guards |
| `command == 'submit'` | Заказ → Подтвердить выполнение (существующая проверка передачи и отчёт) | Original owner, nonce, FSM, CAS, service guards |

FSM: `state.set_state('fulfill:' + step if step in PROMPTS else None)`


Buttons (source inventory; callbacks remain compatible):

- `('← Назад', f'ff:{nonce}:page:{page - 1}')`
- `('Далее →', f'ff:{nonce}:page:{page + 1}')`
- `('✅ Отметить выполнение', f'ff:{nonce}:submit')`
- `('Продолжить', f'ff:{nonce}:continue')`
- `('🔄 Повторить чтение', f'ff:{nonce}:refresh')`
- `('Операция', 'ops:show:' + ident)`
- `('Заказ', 'order:' + form['id'])`

### gifts_panel.py

| Existing function / route | New destination / access | Checks retained |
|---|---|---|
| `command == 'home'` | Подарки → Товары / История / Настроить; экстренная остановка на корневом экране | Original owner, nonce, FSM, CAS, service guards |
| `command == 'refresh'` | Подарки → Товары / История / Настроить; экстренная остановка на корневом экране | Original owner, nonce, FSM, CAS, service guards |
| `command == 'catalog'` | Подарки → Товары / История / Настроить; экстренная остановка на корневом экране | Original owner, nonce, FSM, CAS, service guards |
| `command == 'bindings'` | Подарки → Товары / История / Настроить; экстренная остановка на корневом экране | Original owner, nonce, FSM, CAS, service guards |
| `command == 'stop'` | Подарки → Товары / История / Настроить; экстренная остановка на корневом экране | Original owner, nonce, FSM, CAS, service guards |
| `command in {'limits', 'description'}` | Подарки → Товары / История / Настроить; экстренная остановка на корневом экране | Original owner, nonce, FSM, CAS, service guards |
| `command == 'save_limits'` | Подарки → Товары / История / Настроить; экстренная остановка на корневом экране | Original owner, nonce, FSM, CAS, service guards |
| `command == 'list'` | Подарки → Товары / История / Настроить; экстренная остановка на корневом экране | Original owner, nonce, FSM, CAS, service guards |
| `command in {'job', 'reconcile', 'refundcheck'}` | Подарки → Товары / История / Настроить; экстренная остановка на корневом экране | Original owner, nonce, FSM, CAS, service guards |
| `command == 'confirmbind'` | Подарки → Товары / История / Настроить; экстренная остановка на корневом экране | Original owner, nonce, FSM, CAS, service guards |
| `command in {'retrycheck', 'confirmretry'}` | Подарки → Товары / История / Настроить; экстренная остановка на корневом экране | Original owner, nonce, FSM, CAS, service guards |
| `command == 'item'` | Подарки → Товары / История / Настроить; экстренная остановка на корневом экране | Original owner, nonce, FSM, CAS, service guards |
| `command == 'bind'` | Подарки → Товары / История / Настроить; экстренная остановка на корневом экране | Original owner, nonce, FSM, CAS, service guards |
| `command == 'off'` | Подарки → Товары / История / Настроить; экстренная остановка на корневом экране | Original owner, nonce, FSM, CAS, service guards |
| `command == 'enable'` | Подарки → Товары / История / Настроить; экстренная остановка на корневом экране | Original owner, nonce, FSM, CAS, service guards |
| `command == 'arm'` | Подарки → Товары / История / Настроить; экстренная остановка на корневом экране | Original owner, nonce, FSM, CAS, service guards |
| `current == 'gifts:description'` | Подарки → Товары / История / Настроить; экстренная остановка на корневом экране | Original owner, nonce, FSM, CAS, service guards |
| `command != 'job'` | Подарки → Товары / История / Настроить; экстренная остановка на корневом экране | Original owner, nonce, FSM, CAS, service guards |
| `command == 'retrycheck'` | Подарки → Товары / История / Настроить; экстренная остановка на корневом экране | Original owner, nonce, FSM, CAS, service guards |
| `command == 'limits'` | Подарки → Товары / История / Настроить; экстренная остановка на корневом экране | Original owner, nonce, FSM, CAS, service guards |
| `command == 'reconcile'` | Подарки → Товары / История / Настроить; экстренная остановка на корневом экране | Original owner, nonce, FSM, CAS, service guards |

FSM: `state.set_state(None)`, `state.set_state('gifts:' + command)`


Buttons (source inventory; callbacks remain compatible):

- `('Открыть заказ', 'order:' + ident)`
- `('К подаркам', 'gft:home')`
- `('Подтвердить код / обновить привязку', 'gft:bind:' + ident)`
- `('Далее', f'gft:list:{group}:{page + 1}')`
- `('Назад', f'gft:list:{group}:{page - 1}')`
- `('Повторить только безопасную проверку', 'gft:retrycheck:' + ident)`
- `('🔎 Сверить доставку (только чтение)', 'gft:reconcile:' + ident)`
- `('🔎 Проверить возврат на сервере', 'gft:refundcheck:' + ident)`
- `('Сохранить', 'gft:save_limits:' + nonce)`
- `('Отмена', 'gft:home')`
- `('🔄 Подключение / баланс / каталог', 'gft:refresh')`
- `('📚 Каталог', 'gft:catalog')`
- `('📦 Привязанные лоты', 'gft:bindings')`
- `('➕ Выбрать лот', 'items')`
- `('💰 Лимиты', 'gft:limits')`
- `('📋 Проверить описание', 'gft:description')`
- `('✅ Успешные', 'gft:list:success:0')`
- `('⚠️ Ошибки', 'gft:list:failed:0')`
- `('↩️ Ожидают возврата', 'gft:list:refund:0')`
- `('Возвращены', 'gft:list:refunded:0')`
- `('❓ UNCERTAIN', 'gft:list:uncertain:0')`
- `('⛔ Экстренно остановить покупки', 'gft:stop')`
- `('🔄 Проверить Telegram', 'gft:refresh')`
- `('Назад', 'gft:home')`
- `('Операции UNCERTAIN', 'gft:list:uncertain:0')`
- `('К привязке', 'gft:item:' + p['item'])`
- `('Заказ', 'gft:job:' + ident)`
- `('Остановить лот', 'gft:off:' + ident)`
- `('Включить для будущих заказов', 'gft:enable:' + ident)`
- `('Подтвердить пакет', 'gft:confirmbind:' + nonce)`
- `('Отмена', 'gft:item:' + ident)`
- `('Назад', 'gft:item:' + ident)`
- `('Разрешаю подарки; завершать вручную', f'gft:arm:{ident}:{nonce}:0')`
- `('Разрешаю подарки + допустимый авто-SENT', f'gft:arm:{ident}:{nonce}:1')`
- `('К лоту', 'gft:item:' + ident)`
- `('Подтверждаю', f'gft:confirmretry:{ident}:{nonce}')`
- `('Отмена', 'gft:job:' + ident)`
- `('К лоту', 'item:' + ident)`

### interface_cleanup_panel.py

| Existing function / route | New destination / access | Checks retained |
|---|---|---|
| `action == 'ic:preview'` | Ещё → Инструменты → Очистка интерфейса (с подтверждением) | Original owner, nonce, FSM, CAS, service guards |
| `action.startswith('ic:commit:')` | Ещё → Инструменты → Очистка интерфейса (с подтверждением) | Original owner, nonce, FSM, CAS, service guards |

FSM: `state.set_state('interface_cleanup:confirm')`


Buttons (source inventory; callbacks remain compatible):

- `('Удалить выбранные сообщения', 'ic:commit:' + nonce)`
- `('Новое превью очистки', 'ic:preview')`
- `('Настройки', 'settings')`
- `('Отмена', 'settings')`

### item_category_picker.py

| Existing function / route | New destination / access | Checks retained |
|---|---|---|
| `command == 'next'` | Товары → Фильтры → Категория; мастер → игра / категория | Original owner, nonce, FSM, CAS, service guards |
| `command == 'page'` | Товары → Фильтры → Категория; мастер → игра / категория | Original owner, nonce, FSM, CAS, service guards |
| `command != 'pick'` | Товары → Фильтры → Категория; мастер → игра / категория | Original owner, nonce, FSM, CAS, service guards |

FSM: `state.set_state('itemlist:category_search')`, `state.set_state(None)`


Buttons (source inventory; callbacks remain compatible):

- `('Отмена', 'itemfilters')`
- `('← Назад', f'icp:{nonce}:page:{page - 1}')`
- `('Далее →', f'icp:{nonce}:page:{page + 1}')`
- `('Другая игра', 'il:pickcategory')`
- `('Далее →', f'icp:{nonce}:next:0')`

### item_panel.py

| Existing function / route | New destination / access | Checks retained |
|---|---|---|

Buttons (source inventory; callbacks remain compatible):

- `('← Фото', f"itemmedia:{view['nonce']}:{index - 1}")`
- `('Фото →', f"itemmedia:{view['nonce']}:{index + 1}")`
- `('📦 Лот', 'item:' + view['id'])`
- `('🖼 Просмотреть фото', f'itemmedia:{nonce}:0')`

### keyboards.py

| Existing function / route | New destination / access | Checks retained |
|---|---|---|

Buttons (source inventory; callbacks remain compatible):

- `('👤 Профиль', 'profile')`
- `('📊 Статистика', 'stats:7')`
- `('📦 Мои товары', 'items')`
- `('➕ Создать товар', 'create')`
- `('🛒 Заказы', 'orders')`
- `('💬 Сообщения', 'chats')`
- `('⚡ Автоматизация', 'automation')`
- `('⚙️ Настройки', 'settings')`
- `('💾 Черновики', 'drafts')`
- `('🩺 Диагностика', 'diagnostics')`
- `('🎁 Telegram-подарки', 'gft:home')`
- `('🧾 Результаты операций', 'result:list:0')`
- `('📦 Выдано — подтвердить', 'delivered:0')`

### listings_panel.py

| Existing function / route | New destination / access | Checks retained |
|---|---|---|
| `kind not in {'items', 'chats'}` | Товары / Сообщения → список → поиск, фильтры, страницы | Original owner, nonce, FSM, CAS, service guards |
| `kind == 'items'` | Товары / Сообщения → список → поиск, фильтры, страницы | Original owner, nonce, FSM, CAS, service guards |
| `action == 'il:pickcategory'` | Товары / Сообщения → список → поиск, фильтры, страницы | Original owner, nonce, FSM, CAS, service guards |
| `action == 'il:reset'` | Товары / Сообщения → список → поиск, фильтры, страницы | Original owner, nonce, FSM, CAS, service guards |
| `current == 'itemlist:category_search'` | Товары / Сообщения → список → поиск, фильтры, страницы | Original owner, nonce, FSM, CAS, service guards |
| `current == 'itemlist:search'` | Товары / Сообщения → список → поиск, фильтры, страницы | Original owner, nonce, FSM, CAS, service guards |
| `current == 'itemlist:price'` | Товары / Сообщения → список → поиск, фильтры, страницы | Original owner, nonce, FSM, CAS, service guards |
| `current == 'itemlist:category'` | Товары / Сообщения → список → поиск, фильтры, страницы | Original owner, nonce, FSM, CAS, service guards |

FSM: `state.set_state('itemlist:' + field)`, `state.set_state(None)`


Buttons (source inventory; callbacks remain compatible):

- `('📦 Мои товары', 'item')`
- `('💬 Диалоги', 'chat')`
- `('Далее →', f'listnext:{kind}:{next_nonce}')`
- `('🔎 Фильтры', 'itemfilters')`
- `('Отмена', 'itemfilters')`

### message_notice.py

| Existing function / route | New destination / access | Checks retained |
|---|---|---|

Buttons (source inventory; callbacks remain compatible):

- `('✉️ Ответить', 'reply:' + chat_id)`
- `('📖 Диалог', 'chat:' + chat_id)`
- `('✅ Прочитано', 'readchat:' + chat_id)`

### operation_results.py

| Existing function / route | New destination / access | Checks retained |
|---|---|---|
| `kind in {'delivery', 'delivery_text'}` | Уведомление / Ещё → Инструменты → Результаты действий → объект | Original owner, nonce, FSM, CAS, service guards |
| `kind in {'delivery', 'delivery_text', 'fulfill'}` | Уведомление / Ещё → Инструменты → Результаты действий → объект | Original owner, nonce, FSM, CAS, service guards |
| `kind in {'create', 'publish', 'edit', 'discount'}` | Уведомление / Ещё → Инструменты → Результаты действий → объект | Original owner, nonce, FSM, CAS, service guards |
| `kind == 'delete'` | Уведомление / Ещё → Инструменты → Результаты действий → объект | Original owner, nonce, FSM, CAS, service guards |
| `kind == 'create'` | Уведомление / Ещё → Инструменты → Результаты действий → объект | Original owner, nonce, FSM, CAS, service guards |
| `kind != 'fulfill'` | Уведомление / Ещё → Инструменты → Результаты действий → объект | Original owner, nonce, FSM, CAS, service guards |
| `kind != 'delete'` | Уведомление / Ещё → Инструменты → Результаты действий → объект | Original owner, nonce, FSM, CAS, service guards |

Buttons (source inventory; callbacks remain compatible):

- `('🔄 Обновить результат', 'result:show:' + ident)`
- `('Все результаты', 'result:list:0')`
- `('← Назад', f'result:list:{page - 1}')`
- `('Далее →', f'result:list:{page + 1}')`
- `('Подарочный заказ и безопасная сверка', 'gft:job:' + row['target'])`
- `('Безопасная сверка', 'ops:show:' + ident)`
- `('Заказ и состояние склада', 'order:' + row['target'])`
- `('Выдано — ожидает подтверждения', 'delivered:0')`
- `('📦 Лот и доступные действия', 'item:' + target)`
- `('📚 Склад / перенос из мастера', 'stock:view:' + target)`
- `('Открыть актуальную карточку', 'item:' + row['target'])`

### operations_panel.py

| Existing function / route | New destination / access | Checks retained |
|---|---|---|
| `command == 'apply'` | Ещё → Инструменты → История операций / Неопределённые операции | Original owner, nonce, FSM, CAS, service guards |
| `command == 'show'` | Ещё → Инструменты → История операций / Неопределённые операции | Original owner, nonce, FSM, CAS, service guards |
| `command == 'confirm'` | Ещё → Инструменты → История операций / Неопределённые операции | Original owner, nonce, FSM, CAS, service guards |
| `command not in {'stop', 'confirm'}` | Ещё → Инструменты → История операций / Неопределённые операции | Original owner, nonce, FSM, CAS, service guards |

FSM: `state.set_state('resolve:reason')`, `state.set_state(None)`, `state.set_state('resolve:message')`


Buttons (source inventory; callbacks remain compatible):

- `('← Назад', f'ops:list:{status}:{page - 1}')`
- `('Далее →', f'ops:list:{status}:{page + 1}')`
- `('Неопределённые', 'ops:list:UNCERTAIN:0')`
- `('Все операции', 'ops:list:ALL:0')`
- `('История операций', 'ops:list:ALL:0')`
- `('⛔ Остановить без повтора', 'ops:stop:' + ident)`
- `('✅ Сохранить решение', 'ops:apply:' + data['nonce'])`
- `('Операция', 'ops:show:' + data['id'])`
- `('🔎 Сверить успешный результат', 'ops:confirm:' + ident)`

### order_finance_panel.py

| Existing function / route | New destination / access | Checks retained |
|---|---|---|
| `command == 'card'` | Заказ → Подробнее → Себестоимость и прибыль | Original owner, nonce, FSM, CAS, service guards |
| `command == 'commit'` | Заказ → Подробнее → Себестоимость и прибыль | Original owner, nonce, FSM, CAS, service guards |
| `command in {'sources', 'allocs'}` | Заказ → Подробнее → Себестоимость и прибыль | Original owner, nonce, FSM, CAS, service guards |
| `command == 'pick'` | Заказ → Подробнее → Себестоимость и прибыль | Original owner, nonce, FSM, CAS, service guards |
| `command in {'edit', 'clear'}` | Заказ → Подробнее → Себестоимость и прибыль | Original owner, nonce, FSM, CAS, service guards |
| `command == 'review'` | Заказ → Подробнее → Себестоимость и прибыль | Original owner, nonce, FSM, CAS, service guards |
| `command == 'sources'` | Заказ → Подробнее → Себестоимость и прибыль | Original owner, nonce, FSM, CAS, service guards |
| `command == 'clear'` | Заказ → Подробнее → Себестоимость и прибыль | Original owner, nonce, FSM, CAS, service guards |
| `command == 'allocs'` | Заказ → Подробнее → Себестоимость и прибыль | Original owner, nonce, FSM, CAS, service guards |

FSM: `state.set_state('profit:confirm')`, `state.set_state(None)`, `state.set_state('profit:amount')`


Buttons (source inventory; callbacks remain compatible):

- `('Себестоимость заказа', 'pf:edit:cost:' + ident)`
- `('Остальные расходы', 'pf:edit:other_expenses:' + ident)`
- `('Распределить PREMIUM/удержания', f'pf:sources:{ident}:0')`
- `('Мои распределения', f'pf:allocs:{ident}:0')`
- `('Очистить себестоимость', 'pf:clear:cost:' + ident)`
- `('Очистить прочие расходы', 'pf:clear:other_expenses:' + ident)`
- `('К заказу', 'order:' + ident)`
- `('Проверить полноту и итог', 'pf:review:' + ident)`
- `('← Назад', f'pf:{command}:{ident}:{page - 1}')`
- `('Далее →', f'pf:{command}:{ident}:{page + 1}')`
- `('Учёт заказа', 'pf:card:' + ident)`
- `('Сохранить локально', 'pf:commit:' + nonce)`
- `('Отмена', 'pf:card:' + pending['order_id'])`

### orders_panel.py

| Existing function / route | New destination / access | Checks retained |
|---|---|---|
| `action == 'orders'` | Заказы → карточка → Подробнее (история, заметки, учёт, сверка) | Original owner, nonce, FSM, CAS, service guards |
| `action.startswith('orderpage:')` | Заказы → карточка → Подробнее (история, заметки, учёт, сверка) | Original owner, nonce, FSM, CAS, service guards |
| `action == 'orderfilters'` | Заказы → карточка → Подробнее (история, заметки, учёт, сверка) | Original owner, nonce, FSM, CAS, service guards |
| `action.startswith('order:')` | Заказы → карточка → Подробнее (история, заметки, учёт, сверка) | Original owner, nonce, FSM, CAS, service guards |
| `command == 'search'` | Заказы → карточка → Подробнее (история, заметки, учёт, сверка) | Original owner, nonce, FSM, CAS, service guards |
| `command == 'entry'` | Заказы → карточка → Подробнее (история, заметки, учёт, сверка) | Original owner, nonce, FSM, CAS, service guards |
| `command == 'history'` | Заказы → карточка → Подробнее (история, заметки, учёт, сверка) | Original owner, nonce, FSM, CAS, service guards |
| `command == 'apply'` | Заказы → карточка → Подробнее (история, заметки, учёт, сверка) | Original owner, nonce, FSM, CAS, service guards |
| `command in {'prepare', 'stock'}` | Заказы → карточка → Подробнее (история, заметки, учёт, сверка) | Original owner, nonce, FSM, CAS, service guards |
| `command in {'note', 'problem', 'clear'}` | Заказы → карточка → Подробнее (история, заметки, учёт, сверка) | Original owner, nonce, FSM, CAS, service guards |
| `current == 'order:search'` | Заказы → карточка → Подробнее (история, заметки, учёт, сверка) | Original owner, nonce, FSM, CAS, service guards |
| `current == 'order:stock'` | Заказы → карточка → Подробнее (история, заметки, учёт, сверка) | Original owner, nonce, FSM, CAS, service guards |
| `command == 'prepare'` | Заказы → карточка → Подробнее (история, заметки, учёт, сверка) | Original owner, nonce, FSM, CAS, service guards |
| `command == 'clear'` | Заказы → карточка → Подробнее (история, заметки, учёт, сверка) | Original owner, nonce, FSM, CAS, service guards |
| `command == 'note'` | Заказы → карточка → Подробнее (история, заметки, учёт, сверка) | Original owner, nonce, FSM, CAS, service guards |
| `command == 'problem'` | Заказы → карточка → Подробнее (история, заметки, учёт, сверка) | Original owner, nonce, FSM, CAS, service guards |

FSM: `state.set_state(None)`, `state.set_state('order:search')`, `state.set_state('order:stock')`, `state.set_state('order:note')`


Buttons (source inventory; callbacks remain compatible):

- `('← Назад', f'delivered:{page - 1}')`
- `('Далее →', f'delivered:{page + 1}')`
- `('← Назад', f'orderpage:{page - 1}:{selected}')`
- `('Далее →', f'orderpage:{page + 1}:{selected}')`
- `('🔄 Проверить статус', 'order:' + ident)`
- `('🛒 К списку', 'orders')`
- `('← Назад', f'od:history:{ident}:{page - 1}')`
- `('Далее →', f'od:history:{ident}:{page + 1}')`
- `('Подтверждение выполнения', 'ff:start:' + row['id'])`
- `('🔎 Поиск', 'od:search')`
- `('Фильтры', 'orderfilters')`
- `('Сбросить поиск', 'orders')`
- `('🔄 Сверить', 'sync')`
- `('💬 Диалог', 'chat:' + chat)`
- `('✉️ Написать', 'reply:' + chat)`
- `('✅ Я выполнил заказ', 'ff:start:' + ident)`
- `('🎁 Подарок / сверка / возврат', 'gft:job:' + ident)`
- `('Операция выдачи', 'ops:show:' + operation[0]['id'])`
- `('📝 История', f'od:history:{ident}:0')`
- `('Заметка', 'od:note:' + ident)`
- `('❗ Локальная проблема', 'od:problem:' + ident)`
- `('🧾 Себестоимость и прибыль', 'pf:card:' + ident)`
- `('Снять локальную отметку', 'od:clear:' + ident)`
- `('📦 Выдать из запаса', 'od:prepare:' + ident)`
- `('➕ Добавить уникальные коды', 'od:stock:' + ident)`
- `('✅ Подтвердить', 'od:apply:' + nonce)`
- `('К заказу', 'order:' + ident)`
- `('К заказу', 'order:' + rows[0]['order_id'])`
- `('К заказу', 'order:' + target)`

### profile_panel.py

| Existing function / route | New destination / access | Checks retained |
|---|---|---|

Buttons (source inventory; callbacks remain compatible):

- `('outgoing', 'Продаж')`
- `('incoming', 'Покупок')`
- `('🔄 Проверить соединение', 'profile:check')`
- `('message to edit not found', "message can't be edited")`

### promotions_panel.py

| Existing function / route | New destination / access | Checks retained |
|---|---|---|
| `action == 'promo:expenses'` | Ещё → Продвижение — проверка → параметры / история | Original owner, nonce, FSM, CAS, service guards |
| `action in {'promotions', 'promo:stop'}` | Ещё → Продвижение — проверка → параметры / история | Original owner, nonce, FSM, CAS, service guards |
| `action == 'promo:stop'` | Ещё → Продвижение — проверка → параметры / история | Original owner, nonce, FSM, CAS, service guards |
| `command == 'toggle'` | Ещё → Продвижение — проверка → параметры / история | Original owner, nonce, FSM, CAS, service guards |
| `command == 'mode'` | Ещё → Продвижение — проверка → параметры / история | Original owner, nonce, FSM, CAS, service guards |
| `command == 'selection'` | Ещё → Продвижение — проверка → параметры / история | Original owner, nonce, FSM, CAS, service guards |
| `command == 'edit'` | Ещё → Продвижение — проверка → параметры / история | Original owner, nonce, FSM, CAS, service guards |
| `command == 'save'` | Ещё → Продвижение — проверка → параметры / история | Original owner, nonce, FSM, CAS, service guards |

FSM: `state.set_state(None)`, `state.set_state('promotion:value')`


Buttons (source inventory; callbacks remain compatible):

- `('Переключить политику', f'pp:{nonce}:toggle:enabled')`
- `('Режим', f'pp:{nonce}:mode')`
- `('Ночной режим', f'pp:{nonce}:toggle:night_enabled')`
- `('Выбор лотов', f'pp:{nonce}:selection')`
- `('💾 Сохранить', f'pp:{nonce}:save')`
- `('⛔ Выключить', 'promo:stop')`
- `('Выбрать товар и проверить', 'items')`
- `('История расходов', 'aq:list:expenses:0')`
- `('История dry-run', 'aq:list:evaluations:0')`
- `('Выбранные / исключённые', 'aq:list:targets:0')`
- `('Очередь проверок', 'aq:list:schedule:0')`
- `('Включить лот', f'pi:{nonce}:target:include')`
- `('Исключить', f'pi:{nonce}:target:exclude')`
- `('По общему правилу', f'pi:{nonce}:target:default')`
- `('Настройки продвижения', 'promotions')`
- `('Настройки', 'promotions')`
- `('Лот', 'promo:item:' + view['id'])`

### replies_panel.py

| Existing function / route | New destination / access | Checks retained |
|---|---|---|
| `command == 'add'` | Сообщения → ответ с подтверждением; Ещё → Настройки → Быстрые ответы | Original owner, nonce, FSM, CAS, service guards |
| `command == 'view'` | Сообщения → ответ с подтверждением; Ещё → Настройки → Быстрые ответы | Original owner, nonce, FSM, CAS, service guards |
| `command == 'list'` | Сообщения → ответ с подтверждением; Ещё → Настройки → Быстрые ответы | Original owner, nonce, FSM, CAS, service guards |
| `command == 'edit'` | Сообщения → ответ с подтверждением; Ещё → Настройки → Быстрые ответы | Original owner, nonce, FSM, CAS, service guards |
| `command == 'delete'` | Сообщения → ответ с подтверждением; Ещё → Настройки → Быстрые ответы | Original owner, nonce, FSM, CAS, service guards |
| `command == 'remove'` | Сообщения → ответ с подтверждением; Ещё → Настройки → Быстрые ответы | Original owner, nonce, FSM, CAS, service guards |
| `command == 'type'` | Сообщения → ответ с подтверждением; Ещё → Настройки → Быстрые ответы | Original owner, nonce, FSM, CAS, service guards |
| `command == 'use'` | Сообщения → ответ с подтверждением; Ещё → Настройки → Быстрые ответы | Original owner, nonce, FSM, CAS, service guards |

FSM: `state.set_state('reply' if chat_id else None)`, `state.set_state(None)`, `state.set_state('quick:name')`, `state.set_state('reply')`, `state.set_state('quick:text')`


Buttons (source inventory; callbacks remain compatible):

- `('➕ Добавить ответ', f'quick:{nonce}:add')`
- `('✍️ Свой текст', f'quick:{nonce}:type')`
- `('✉️ Использовать', f"quick:{nonce}:use:{value['id']}")`
- `('✏️ Изменить', f"quick:{nonce}:edit:{value['id']}")`
- `('🗑 Удалить', f"quick:{nonce}:delete:{value['id']}")`
- `('← К списку', f"quick:{nonce}:list:{value['id']}")`
- `('✅ Отправить', 'commit:' + nonce)`
- `('Удалить', f"quick:{nonce}:remove:{value['id']}")`

### router.py

| Existing function / route | New destination / access | Checks retained |
|---|---|---|
| `action == 'home'` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `action == 'create'` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `current == 'wizard'` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `action in {'profile', 'profile:check', 'diagnostics', 'backup'}` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `action.startswith(('item:', 'order:', 'chat:', 'result:', 'delivered:', 'stats:', 'st:', 'sd:', 'sp:', 'stock:list:', 'ops:show:', 'aq:list:', 'aq:chain:', 'aq:target:', 'aq:evaluation:', 'gft:item:', 'gft:job:', 'gft:list:', 'gft:catalog', 'gft:bindings', 'gft:reconcile:', 'gft:refundcheck:'))` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `action.startswith('agv:')` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `current == 'stock:input'` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `action.startswith('gft:')` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `action.startswith('pf:')` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `action.startswith('stock:')` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `action.startswith('result:')` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `current == 'draftlist:search'` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `action.startswith('delivered:')` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `action.startswith('ic:')` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `action == 'quickreplies'` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `action.startswith('quick:')` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `action.startswith('ops:')` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `action.startswith('aq:')` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `action.startswith('ar:')` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `action == 'promotions'` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `action.startswith(('promo:', 'pp:', 'pi:'))` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `current == 'reply'` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `current == 'discount'` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `action == 'automation'` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `action.startswith(('auto:', 'ap:'))` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `action.startswith('ff:')` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `current == 'editphoto'` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `action.startswith('w:')` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `current in {'reply', 'edit'}` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `action == 'cancel_wizard'` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `action == 'drafts'` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `action.startswith('dl:')` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `action.startswith('draft:')` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `action in {'items', 'chats'}` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `action == 'itemfilters'` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `action.startswith('itemfilter:')` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `action == 'il:back'` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `action.startswith('il:')` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `action.startswith('icp:')` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `action.startswith('listnext:')` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `action.startswith('item:')` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `action.startswith('itemmedia:')` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `action.startswith('editfields:')` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `action.startswith('quote:')` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `action.startswith(('discount:', 'editprice:'))` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `action.startswith(('editdesc:', 'editname:', 'editphoto:'))` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `action.startswith(('delete:', 'publish:'))` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `kind == 'publish'` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `action.startswith('commit:')` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `kind == 'editphoto'` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `kind == 'delete'` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `action in {'orders', 'orderfilters'}` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `action.startswith(('orderpage:', 'order:', 'od:'))` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `action.startswith(('chat:', 'chatpage:'))` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `action.startswith('chatmedia:')` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `action.startswith('readchat:')` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `action.startswith('reply:')` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `action == 'sync'` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `action in {'profile', 'profile:check'}` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `action.startswith(('stats:', 'st:', 'sd:'))` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `action == 'settings'` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `action.startswith('sp:')` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `action == 'backup'` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `action == 'profile:check'` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |
| `action == 'diagnostics'` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Original owner, nonce, FSM, CAS, service guards |

FSM: `state.set_state(None)`, `state.set_state('wizard')`, `state.set_state('discount')`, `state.set_state('editphoto' if kind == 'editphoto' else 'edit')`


Buttons (source inventory; callbacks remain compatible):

- `('message to edit not found', "message can't be edited")`
- `('auto:', 'ap:')`
- `('✅ Подтвердить цену', 'commit:' + nonce)`
- `('✅ Подтвердить', 'commit:' + nonce)`
- `('✅ Заменить фотографии', 'commit:' + nonce)`
- `('Открыть серверный лот', 'item:' + binding[0]['remote_id'])`
- `('Все статусы', 'itemfilter:ALL')`
- `('Название', 'il:search')`
- `('Цена', 'il:price')`
- `('Категория', 'il:pickcategory')`
- `('Категория · ввод ID / снять', 'il:category')`
- `('PREMIUM', 'il:PREMIUM')`
- `('Обычные', 'il:DEFAULT')`
- `('Любой приоритет', 'il:ALL')`
- `('Сбросить фильтры', 'il:reset')`
- `('К списку', 'il:back')`
- `('📚 Склад', 'stock:view:' + ident)`
- `('🎁 Telegram-подарок', 'gft:item:' + ident)`
- `('💰 Цена / Скидка', 'discount:' + ident)`
- `('📋 Описание', 'editdesc:' + ident)`
- `('📝 Название', 'editname:' + ident)`
- `('🖼 Заменить фото', 'editphoto:' + ident)`
- `('🧩 Атрибуты и данные', 'editfields:' + ident)`
- `('⭐ Стоимость размещения', 'quote:' + ident)`
- `('💰 Финансовый расчёт', 'finance:' + ident)`
- `('⚡ Политика выдачи · dry-run', 'auto:item:' + ident)`
- `('🚀 Продвижение · dry-run', 'promo:item:' + ident)`
- `('🗑 Удалить', 'delete:' + ident)`
- `('📥 Перенести запас из мастера', 'stock:transfer:' + draft_id)`
- `('Бесплатная публикация · превью', 'publish:' + ident)`
- `('discount:', 'editprice:')`
- `('⬅️ Лот', 'item:' + ident)`
- `('delete:', 'publish:')`
- `('chat:', 'chatpage:')`
- `('Подтвердить', 'commit:' + nonce)`
- `('Открыть результат', 'result:show:' + ident)`
- `('🛒 Заказы', 'orders')`
- `('🔄 Сверить API', 'sync')`
- `('📄 Условия продавца · просмотр', 'agv:list:0')`
- `('⚠️ Неопределённые операции', 'ops:list:UNCERTAIN:0')`
- `('🧾 История операций', 'ops:list:ALL:0')`

### settings_panel.py

| Existing function / route | New destination / access | Checks retained |
|---|---|---|
| `action == 'settings'` | Ещё → Настройки → уведомления / ответы / подключения / безопасность / финансы | Original owner, nonce, FSM, CAS, service guards |
| `action == 'sp:notifications'` | Ещё → Настройки → уведомления / ответы / подключения / безопасность / финансы | Original owner, nonce, FSM, CAS, service guards |
| `action.startswith('sp:notify:')` | Ещё → Настройки → уведомления / ответы / подключения / безопасность / финансы | Original owner, nonce, FSM, CAS, service guards |
| `action == 'sp:connections'` | Ещё → Настройки → уведомления / ответы / подключения / безопасность / финансы | Original owner, nonce, FSM, CAS, service guards |
| `action == 'sp:security'` | Ещё → Настройки → уведомления / ответы / подключения / безопасность / финансы | Original owner, nonce, FSM, CAS, service guards |
| `action == 'sp:fulfillment'` | Ещё → Настройки → уведомления / ответы / подключения / безопасность / финансы | Original owner, nonce, FSM, CAS, service guards |

Buttons (source inventory; callbacks remain compatible):

- `('Настройки', 'settings')`
- `('🔐 Подключения', 'sp:connections')`
- `('🛡️ Безопасность', 'sp:security')`
- `('⚡ Автовыдача', 'automation')`
- `('⬆️ Автоподнятие · настройки', 'promotions')`
- `('✅ Подтверждение выполнения', 'sp:fulfillment')`
- `('💬 Ответы и шаблоны', 'quickreplies')`
- `('🔔 Уведомления', 'sp:notifications')`
- `('💬 Правила автоответов', 'ar:menu')`
- `('💰 Финансовые параметры', 'providers')`
- `('📊 Статистика', 'stats:7')`
- `('💾 Резервная копия БД', 'backup')`
- `('🩺 Диагностика', 'diagnostics')`
- `('🧹 Очистка интерфейса', 'ic:preview')`
- `('Подробная диагностика', 'diagnostics')`
- `('Неопределённые операции', 'ops:list:UNCERTAIN:0')`
- `('Заказы', 'orders')`
- `('Автовыдача', 'automation')`

### statistics_panel.py

| Existing function / route | New destination / access | Checks retained |
|---|---|---|

Buttons (source inventory; callbacks remain compatible):

- `('24 ч', 'stats:1')`
- `('48 ч', 'stats:2')`
- `('7 дн', 'stats:7')`
- `('30 дн', 'stats:30')`
- `('Всё', 'stats:0')`
- `('Топ по заказам', f'sd:{days}:bestsellers')`
- `('Топ по выручке', f'sd:{days}:items')`
- `('Категории · заказы', f'sd:{days}:category_counts')`
- `('Категории · выручка', f'sd:{days}:categories')`
- `('Поступления и расходы по транзакциям', f'sd:{days}:transactions')`
- `('Прибыль по учтённым заказам', f'sd:{days}:profit')`
- `('Сверенные комиссии продажи', f'sd:{days}:settlements')`
- `('Подробности и покрытие', f'sd:{days}:details')`

### stock_panel.py

| Existing function / route | New destination / access | Checks retained |
|---|---|---|
| `command == 'view'` | Товары → карточка → Настроить выдачу → Склад | Original owner, nonce, FSM, CAS, service guards |
| `command == 'add'` | Товары → карточка → Настроить выдачу → Склад | Original owner, nonce, FSM, CAS, service guards |
| `command == 'transfer'` | Товары → карточка → Настроить выдачу → Склад | Original owner, nonce, FSM, CAS, service guards |
| `command == 'commit'` | Товары → карточка → Настроить выдачу → Склад | Original owner, nonce, FSM, CAS, service guards |
| `command == 'list'` | Товары → карточка → Настроить выдачу → Склад | Original owner, nonce, FSM, CAS, service guards |
| `command == 'resume'` | Товары → карточка → Настроить выдачу → Склад | Original owner, nonce, FSM, CAS, service guards |
| `command == 'resumeok'` | Товары → карточка → Настроить выдачу → Склад | Original owner, nonce, FSM, CAS, service guards |

FSM: `state.set_state('stock:confirm')`, `state.set_state('stock:input')`, `state.set_state(None)`


Buttons (source inventory; callbacks remain compatible):

- `('➕ Загрузить запас', 'stock:add:' + ident)`
- `('⚡ Настроить политику', 'auto:item:' + ident)`
- `('📦 К товару', 'item:' + ident)`
- `('📥 Перенести запас из мастера', 'stock:transfer:' + draft)`
- `('← Назад', f'stock:list:{ident}:{selected}:{page - 1}')`
- `('Далее →', f'stock:list:{ident}:{selected}:{page + 1}')`
- `('Подготовить возобновление', 'stock:resume:' + ident)`
- `('✅ Сохранить в склад', 'stock:commit:' + nonce)`
- `('Отмена', 'stock:view:' + ident)`
- `('✅ Возобновить', 'stock:resumeok:' + nonce)`
- `('К складу', 'stock:view:' + ident)`

### wizard.py

| Existing function / route | New destination / access | Checks retained |
|---|---|---|
| `step == 'game'` | Товары → Создать / Редактировать → существующий устойчивый мастер | Original owner, nonce, FSM, CAS, service guards |
| `step not in {'game', 'preview', 'agreements_wait'}` | Товары → Создать / Редактировать → существующий устойчивый мастер | Original owner, nonce, FSM, CAS, service guards |
| `step == 'category'` | Товары → Создать / Редактировать → существующий устойчивый мастер | Original owner, nonce, FSM, CAS, service guards |
| `step != 'preview'` | Товары → Создать / Редактировать → существующий устойчивый мастер | Original owner, nonce, FSM, CAS, service guards |
| `action == 'conditions_done'` | Товары → Создать / Редактировать → существующий устойчивый мастер | Original owner, nonce, FSM, CAS, service guards |
| `step == 'conditions'` | Товары → Создать / Редактировать → существующий устойчивый мастер | Original owner, nonce, FSM, CAS, service guards |
| `step == 'fields'` | Товары → Создать / Редактировать → существующий устойчивый мастер | Original owner, nonce, FSM, CAS, service guards |
| `step == 'obtaining'` | Товары → Создать / Редактировать → существующий устойчивый мастер | Original owner, nonce, FSM, CAS, service guards |
| `step in {'name', 'description'}` | Товары → Создать / Редактировать → существующий устойчивый мастер | Original owner, nonce, FSM, CAS, service guards |
| `step == 'agreements_wait'` | Товары → Создать / Редактировать → существующий устойчивый мастер | Original owner, nonce, FSM, CAS, service guards |
| `action == 'agreements_check'` | Товары → Создать / Редактировать → существующий устойчивый мастер | Original owner, nonce, FSM, CAS, service guards |
| `action == 'preview_page'` | Товары → Создать / Редактировать → существующий устойчивый мастер | Original owner, nonce, FSM, CAS, service guards |
| `step == 'preview'` | Товары → Создать / Редактировать → существующий устойчивый мастер | Original owner, nonce, FSM, CAS, service guards |
| `step == 'description'` | Товары → Создать / Редактировать → существующий устойчивый мастер | Original owner, nonce, FSM, CAS, service guards |
| `step == 'photos'` | Товары → Создать / Редактировать → существующий устойчивый мастер | Original owner, nonce, FSM, CAS, service guards |
| `action == 'preview_return'` | Товары → Создать / Редактировать → существующий устойчивый мастер | Original owner, nonce, FSM, CAS, service guards |
| `step == 'price'` | Товары → Создать / Редактировать → существующий устойчивый мастер | Original owner, nonce, FSM, CAS, service guards |
| `action == 'finance_open'` | Товары → Создать / Редактировать → существующий устойчивый мастер | Original owner, nonce, FSM, CAS, service guards |
| `step == 'name'` | Товары → Создать / Редактировать → существующий устойчивый мастер | Original owner, nonce, FSM, CAS, service guards |
| `step == 'finance_cost'` | Товары → Создать / Редактировать → существующий устойчивый мастер | Original owner, nonce, FSM, CAS, service guards |
| `action in {'finance_method', 'finance_cost', 'finance_done', 'finance_clear_cost', 'finance_clear_method', 'finance_refresh'}` | Товары → Создать / Редактировать → существующий устойчивый мастер | Original owner, nonce, FSM, CAS, service guards |
| `step == 'finance'` | Товары → Создать / Редактировать → существующий устойчивый мастер | Original owner, nonce, FSM, CAS, service guards |
| `action == 'finance_method'` | Товары → Создать / Редактировать → существующий устойчивый мастер | Original owner, nonce, FSM, CAS, service guards |
| `step == 'delivery_data'` | Товары → Создать / Редактировать → существующий устойчивый мастер | Original owner, nonce, FSM, CAS, service guards |
| `action == 'finance_cost'` | Товары → Создать / Редактировать → существующий устойчивый мастер | Original owner, nonce, FSM, CAS, service guards |
| `action in {'conditions_next', 'conditions_prev', 'conditions_done'}` | Товары → Создать / Редактировать → существующий устойчивый мастер | Original owner, nonce, FSM, CAS, service guards |
| `action == 'conditions_next'` | Товары → Создать / Редактировать → существующий устойчивый мастер | Original owner, nonce, FSM, CAS, service guards |
| `action == 'finance_done'` | Товары → Создать / Редактировать → существующий устойчивый мастер | Original owner, nonce, FSM, CAS, service guards |
| `action == 'conditions_prev'` | Товары → Создать / Редактировать → существующий устойчивый мастер | Original owner, nonce, FSM, CAS, service guards |
| `action == 'conditions_refresh'` | Товары → Создать / Редактировать → существующий устойчивый мастер | Original owner, nonce, FSM, CAS, service guards |
| `step in {'conditions', 'preview'}` | Товары → Создать / Редактировать → существующий устойчивый мастер | Original owner, nonce, FSM, CAS, service guards |
| `action == 'choice_page'` | Товары → Создать / Редактировать → существующий устойчивый мастер | Original owner, nonce, FSM, CAS, service guards |
| `action == 'finance_clear_cost'` | Товары → Создать / Редактировать → существующий устойчивый мастер | Original owner, nonce, FSM, CAS, service guards |
| `current <= 0` | Товары → Создать / Редактировать → существующий устойчивый мастер | Original owner, nonce, FSM, CAS, service guards |
| `current != len(pages) - 1` | Товары → Создать / Редактировать → существующий устойчивый мастер | Original owner, nonce, FSM, CAS, service guards |
| `action == 'back'` | Товары → Создать / Редактировать → существующий устойчивый мастер | Original owner, nonce, FSM, CAS, service guards |
| `step == 'delivery'` | Товары → Создать / Редактировать → существующий устойчивый мастер | Original owner, nonce, FSM, CAS, service guards |
| `action == 'finance_clear_method'` | Товары → Создать / Редактировать → существующий устойчивый мастер | Original owner, nonce, FSM, CAS, service guards |
| `step not in previous` | Товары → Создать / Редактировать → существующий устойчивый мастер | Original owner, nonce, FSM, CAS, service guards |
| `action == 'next'` | Товары → Создать / Редактировать → существующий устойчивый мастер | Original owner, nonce, FSM, CAS, service guards |
| `action == 'choose'` | Товары → Создать / Редактировать → существующий устойчивый мастер | Original owner, nonce, FSM, CAS, service guards |
| `step == 'confirm'` | Товары → Создать / Редактировать → существующий устойчивый мастер | Original owner, nonce, FSM, CAS, service guards |
| `step in {'name', 'fields'}` | Товары → Создать / Редактировать → существующий устойчивый мастер | Original owner, nonce, FSM, CAS, service guards |
| `step in {'fields', 'conditions'}` | Товары → Создать / Редактировать → существующий устойчивый мастер | Original owner, nonce, FSM, CAS, service guards |
| `step in {'game', 'category', 'obtaining'}` | Товары → Создать / Редактировать → существующий устойчивый мастер | Original owner, nonce, FSM, CAS, service guards |
| `step in {'game', 'category'}` | Товары → Создать / Редактировать → существующий устойчивый мастер | Original owner, nonce, FSM, CAS, service guards |
| `action == 'field_keep'` | Товары → Создать / Редактировать → существующий устойчивый мастер | Original owner, nonce, FSM, CAS, service guards |
| `step == 'finance_method'` | Товары → Создать / Редактировать → существующий устойчивый мастер | Original owner, nonce, FSM, CAS, service guards |
| `action == 'field_done'` | Товары → Создать / Редактировать → существующий устойчивый мастер | Original owner, nonce, FSM, CAS, service guards |
| `action == 'photos_done'` | Товары → Создать / Редактировать → существующий устойчивый мастер | Original owner, nonce, FSM, CAS, service guards |
| `action in {'photo_remove', 'photo_replace', 'photo_preview'}` | Товары → Создать / Редактировать → существующий устойчивый мастер | Original owner, nonce, FSM, CAS, service guards |
| `action == 'photo_preview'` | Товары → Создать / Редактировать → существующий устойчивый мастер | Original owner, nonce, FSM, CAS, service guards |
| `action == 'photo_replace'` | Товары → Создать / Редактировать → существующий устойчивый мастер | Original owner, nonce, FSM, CAS, service guards |
| `action == 'confirm_done'` | Товары → Создать / Редактировать → существующий устойчивый мастер | Original owner, nonce, FSM, CAS, service guards |
| `action == 'edit'` | Товары → Создать / Редактировать → существующий устойчивый мастер | Original owner, nonce, FSM, CAS, service guards |
| `action == 'submit'` | Товары → Создать / Редактировать → существующий устойчивый мастер | Original owner, nonce, FSM, CAS, service guards |

FSM: `state.set_state('wizard')`


Buttons (source inventory; callbacks remain compatible):

- `('💾 Сохранить и выйти', 'home')`
- `('❌ Отмена', 'cancel_wizard')`
- `('← Варианты', f'w:{nonce}:choice_page:{page_number - 1}')`
- `('Варианты →', f'w:{nonce}:choice_page:{page_number + 1}')`
- `('⬅️ Назад', f'w:{nonce}:back')`
- `('Вернуться к превью', f'w:{nonce}:preview_return')`
- `('Далее →', f'w:{nonce}:next')`
- `('🔄 Безопасно проверить', f'w:{nonce}:agreements_check')`
- `('← Условия', f'w:{nonce}:conditions_prev')`
- `('Условия →', f'w:{nonce}:conditions_next')`
- `('🔄 Проверить принятие на Playerok', f'w:{nonce}:conditions_refresh')`
- `('Вернуться без подтверждений', f'w:{nonce}:conditions_done')`
- `('Оставить текущее значение', f'w:{nonce}:field_keep')`
- `('✅ Готово', f'w:{nonce}:field_done')`
- `('Готово → Цена', f'w:{nonce}:photos_done')`
- `('Заменить', f'w:{nonce}:photo_replace:{i}')`
- `('Удалить', f'w:{nonce}:photo_remove:{i}')`
- `('Проверить принятие на Playerok', f'w:{parts[1]}:conditions_refresh')`
- `('Продолжить', f'w:{nonce}:confirm_done')`
- `('game_id', 'category')`
- `('category_id', 'obtaining')`
- `('obtaining_type_id', 'fields')`
- `('💳 Выбрать способ вывода', f'w:{nonce}:finance_method')`
- `('✏️ Себестоимость', f'w:{nonce}:finance_cost')`
- `('Очистить', f'w:{nonce}:finance_clear_cost')`
- `('🔄 Обновить комиссии', f'w:{nonce}:finance_refresh')`
- `('Без выбранного способа', f'w:{nonce}:finance_clear_method')`
- `('Вернуться к превью', f'w:{nonce}:finance_done')`
- `('✅ Сохранить изменения на Playerok', f'w:{nonce}:submit')`
- `('✏️ Изменить поля', f'w:{nonce}:edit:fields')`
- `('📄 Условия', f'w:{nonce}:conditions_refresh')`
- `('Создать серверный черновик', f'w:{nonce}:submit')`
- `('📝 Название', f'w:{nonce}:edit:name')`
- `('📋 Описание', f'w:{nonce}:edit:description')`
- `('🖼 Фото', f'w:{nonce}:edit:photos')`
- `('💰 Цена', f'w:{nonce}:edit:price')`
- `('⚡ Выдача', f'w:{nonce}:edit:delivery')`
- `('🎮 Игра', f'w:{nonce}:edit:game')`
- `('📂 Категория', f'w:{nonce}:edit:category')`
- `('📬 Способ передачи', f'w:{nonce}:edit:obtaining')`
- `('🧩 Характеристики', f'w:{nonce}:edit:fields')`
- `('📄 Условия категории', f'w:{nonce}:conditions_refresh')`
- `('💰 Вывод и себестоимость', f'w:{nonce}:finance_open')`
- `('← Превью', f'w:{nonce}:preview_page:{current - 1}')`
- `('Превью →', f'w:{nonce}:preview_page:{current + 1}')`
- `('Открыть результат', 'result:show:' + ident)`

## Этап A: распределение функций

| Панель / функция | Новый путь | Сохранённые проверки |
|---|---|---|
| `router.py` | Домой → пять разделов; Ещё → инструменты; старые callback сохраняются | Владелец, актуальность callback; для записи прежние nonce, CAS, Outbox и серверная проверка |
| `keyboards.py` | Пять постоянных разделов и контекстный возврат | Владелец, актуальность callback; для записи прежние nonce, CAS, Outbox и серверная проверка |
| `listings_panel.py` | Товары / Сообщения → список → поиск, фильтры, страницы | Владелец, актуальность callback; для записи прежние nonce, CAS, Outbox и серверная проверка |
| `item_panel.py` | Товары → карточка → Редактировать / Настроить выдачу / Другие действия | Владелец, актуальность callback; для записи прежние nonce, CAS, Outbox и серверная проверка |
| `orders_panel.py` | Заказы → карточка → Подробнее (история, заметки, учёт, сверка) | Владелец, актуальность callback; для записи прежние nonce, CAS, Outbox и серверная проверка |
| `chat_panel.py` | Сообщения → диалог → Ответить / Быстрый ответ / Прочитано / Ещё | Владелец, актуальность callback; для записи прежние nonce, CAS, Outbox и серверная проверка |
| `replies_panel.py` | Сообщения → ответ с подтверждением; Ещё → Настройки → Быстрые ответы | Владелец, актуальность callback; для записи прежние nonce, CAS, Outbox и серверная проверка |
| `gifts_panel.py` | Подарки → Товары / История / Настроить; экстренная остановка на корневом экране | Владелец, актуальность callback; для записи прежние nonce, CAS, Outbox и серверная проверка |
| `automation_panel.py` | Ещё → Автовыдача → товар → параметры / дополнительные параметры | Владелец, актуальность callback; для записи прежние nonce, CAS, Outbox и серверная проверка |
| `automation_queue.py` | Ещё → Инструменты → Автовыдача / продвижение → журналы | Владелец, актуальность callback; для записи прежние nonce, CAS, Outbox и серверная проверка |
| `stock_panel.py` | Товары → карточка → Настроить выдачу → Склад | Владелец, актуальность callback; для записи прежние nonce, CAS, Outbox и серверная проверка |
| `settings_panel.py` | Ещё → Настройки → уведомления / ответы / подключения / безопасность / финансы | Владелец, актуальность callback; для записи прежние nonce, CAS, Outbox и серверная проверка |
| `statistics_panel.py` | Ещё → Статистика → период / отчёты / графики | Владелец, актуальность callback; для записи прежние nonce, CAS, Outbox и серверная проверка |
| `promotions_panel.py` | Ещё → Продвижение — проверка → параметры / история | Владелец, актуальность callback; для записи прежние nonce, CAS, Outbox и серверная проверка |
| `finance_panel.py` | Товар → Другие действия → Финансовый расчёт; Настройки → Финансовые параметры | Владелец, актуальность callback; для записи прежние nonce, CAS, Outbox и серверная проверка |
| `order_finance_panel.py` | Заказ → Подробнее → Себестоимость и прибыль | Владелец, актуальность callback; для записи прежние nonce, CAS, Outbox и серверная проверка |
| `drafts_panel.py` | Товары → Черновики → продолжить сохранённый мастер | Владелец, актуальность callback; для записи прежние nonce, CAS, Outbox и серверная проверка |
| `fulfillment_panel.py` | Заказ → Подтвердить выполнение (существующая проверка передачи и отчёт) | Владелец, актуальность callback; для записи прежние nonce, CAS, Outbox и серверная проверка |
| `operations_panel.py` | Ещё → Инструменты → История операций / Неопределённые операции | Владелец, актуальность callback; для записи прежние nonce, CAS, Outbox и серверная проверка |
| `operation_results.py` | Уведомление / Ещё → Инструменты → Результаты действий → объект | Владелец, актуальность callback; для записи прежние nonce, CAS, Outbox и серверная проверка |
| `profile_panel.py` | Ещё → Профиль → Проверить соединение | Владелец, актуальность callback; для записи прежние nonce, CAS, Outbox и серверная проверка |
| `agreements_panel.py` | Ещё → Инструменты → Условия продавца (необязательный просмотр) | Владелец, актуальность callback; для записи прежние nonce, CAS, Outbox и серверная проверка |
| `interface_cleanup_panel.py` | Ещё → Инструменты → Очистка интерфейса (с подтверждением) | Владелец, актуальность callback; для записи прежние nonce, CAS, Outbox и серверная проверка |
| `wizard.py` | Товары → Создать / Редактировать → существующий устойчивый мастер | Владелец, актуальность callback; для записи прежние nonce, CAS, Outbox и серверная проверка |
| `auto_replies_panel.py` | Ещё → Настройки → Автоответы → правило / журнал | Владелец, актуальность callback; для записи прежние nonce, CAS, Outbox и серверная проверка |
| `item_category_picker.py` | Товары → Фильтры → Категория; мастер → игра / категория | Владелец, актуальность callback; для записи прежние nonce, CAS, Outbox и серверная проверка |

### Прямые переходы и совместимость

Все существующие callback из исходной инвентаризации остаются в production-маршрутизаторе. Уведомления `order:`, `chat:`, `gft:job:`, `result:show:` открывают новые карточки без изменения бизнес-операций. Карточки операций сохраняют доказательства и отдельную сверку. Старые подтверждения не заменяются навигационными алиасами. Защищённые уведомления и квитанции не редактируются.

### Критерии приёмки

- Пять постоянных корневых пунктов; внимание только по данным SQLite, а соединение только по свежей runtime-проверке.
- Каждой исходной панели и каждому callback назначен путь; вложенные меню сохраняют старые действия.
- Компактные карточки, содержимое запасов скрыто; неизвестный результат остаётся неизвестным.
- Dispatcher: корень, вложенные меню, возвраты, фильтры, старые callback, подтверждения, чужой пользователь.
- Полный pytest, Ruff check/format, pip check, временные миграции, регрессии и отсутствие секретов.
- Никаких live-записей в этом этапе; live-удобство проверяется отдельно владельцем.


## Итоговое дерево навигации

```text
Мой магазин
├─ Товары → поиск / фильтры / страницы / создать / черновики
│  └─ Товар
│     ├─ Редактировать → цена и скидка / название / описание / фото / характеристики
│     ├─ Настроить выдачу → склад / перенос подготовленного запаса / настройки / проверка
│     │  └─ Подарки, только для подходящей категории и способа передачи либо существующей привязки
│     └─ Другие действия → ссылка / расчёт / продвижение / размещение / публикация / удаление / подробности
├─ Заказы → все / оплаченные / требуют внимания / поиск / остальные фильтры / страницы
│  └─ Заказ → написать / выдать (при допуске) / подтвердить выполнение (при допуске)
│     └─ Подробнее → история / заметка / проблема / учёт / запас / сверка / ссылка / обновить
├─ Сообщения → диалог → ответить / быстрый ответ / прочитано / фото / страницы / ещё
│  └─ Ответ → превью → явное подтверждение → ожидание → подтверждённый результат либо неизвестный исход
├─ Подарки → товары с подарками / история выдачи / настроить
│  ├─ Привязка → состав пакета / цена / подтверждение / отдельное разрешение включения / остановка
│  ├─ История → доставлено / в процессе / требует проверки / заблокировано / возвраты
│  │  └─ Заказ → прогресс каждого подарка → история, подтверждения и решение проблемы
│  ├─ Настроить → подключение и баланс / каталог / лимиты / проверка описания
│  └─ Остановить выдачу — на главном экране при включённых настройках; также в настройках
├─ Ещё
│  ├─ Статистика → период → графики и отчёты → покрытие / товары / категории / транзакции / прибыль / комиссии
│  ├─ Автовыдача → товары / настройки → дополнительные параметры / проверка без отправки / остановка
│  ├─ Продвижение — проверка → товар / параметры → интервалы / ночь / бюджет / история
│  ├─ Профиль → проверить соединение / баланс и данные аккаунта
│  ├─ Настройки → уведомления / быстрые ответы / автоответы / подключения / безопасность / финансы / выполнение
│  └─ Инструменты
│     ├─ История и результаты → все операции / неопределённые / результаты действий
│     ├─ Выдача и цепочки → ожидает подтверждения / цепочки / новые заказы / все настройки
│     ├─ Диагностика и данные → диагностика / синхронизация / условия / backup / очистка интерфейса
│     └─ История продвижения → проверки / расходы / товары / расписание
└─ Требует внимания — только при существующих проблемах
```

## Матрица переноса пользовательских сценариев

| Функция | Старый экран | Новый экран и путь | Обязательные проверки |
|---|---|---|---|
| Создать / продолжить товар | Главное меню / черновики | Товары → Создать / Черновики | Устойчивый draft, revision, nonce, условия, итоговое превью, серверные проверки |
| Поиск / цена / категория / приоритет / статус | Товары → фильтры | Товары → Поиск / Фильтры | Исходная валидация, серверный статус/категория, честная постраничная локальная фильтрация |
| Страницы товаров / чатов | Список | Список → Далее | Неповторяемый cursor и nonce; старые страницы не продвигают курсор |
| Цена / скидка / название / описание / фото / данные | Все кнопки в карточке | Товар → Редактировать | Владение, согласия, прежние подтверждения и очередь; фото проверяются заново |
| Публикация / удаление | Карточка | Товар → Другие действия | Только допустимый статус; бесплатный DEFAULT; подтверждение; прежние server/CAS/idempotency |
| Метаданные / счётчики / описание / фото | Длинная карточка | Товар → Другие действия → Подробности | Свежий owned item; счётчик сделок не объявляется продажами |
| Финансовый расчёт / размещение | Карточка | Товар → Другие действия | Прежняя стоимость, комиссии и неполные данные; расчёт не становится оплатой |
| Склад / перенос / карантин / явное возобновление | Карточка | Товар → Настроить выдачу → Склад | Секреты скрыты; import preview, дедупликация, atomic reserve, guard и подтверждение возобновления |
| Настройки выдачи / шаблоны / новые продажи | Автоматизация / карточка | Ещё → Автовыдача или Товар → Настроить выдачу | Текущая readiness отдельно от правок; save/arm revision и nonce сохранены |
| Проверка заказа без отправки / запуск цепочки | Dry-run | Настройки выдачи → Проверить без отправки → отдельное подтверждение запуска | Проверка не создаёт выдачу; запуск сохраняет собственный допуск и подтверждение |
| Поиск / статусы / проблемные заказы | Заказы | Заказы → быстрые вкладки / остальные фильтры / поиск | Исходные серверные статусы; SENT не равен подтверждению покупателя |
| Ручная выдача / выполнение | Перегруженная карточка заказа | Заказ → Выдать / Подтвердить выполнение | Прежние PAID/owner/chat/stock/gift-conflict/attestation/report проверки; cached card не предлагает выдачу |
| История / заметки / проблема / учёт / ссылка | Карточка заказа | Заказ → Подробнее | Исходный CAS локальных заметок; учёт не выдаётся за полную прибыль |
| Выдано — ожидает подтверждения | Главное меню | Ещё → Инструменты → Выдача и цепочки | Доказательство SUCCEEDED и фактический статус склада / receipt; PAID не подменяется SENT |
| Переписка / вложения / прочтение | Диалог с ID на каждой строке | Сообщения → диалог | Хронология и opaque cursor; фото проверяются по действующей странице; mark-read с подтверждением |
| Ответ / шаблон / фото | Переписка и быстрые ответы | Диалог → Ответить / Быстрый ответ → превью | Прежний nonce, origin_event, upload, idempotency и Outbox; очередь не успех |
| Быстрые ответы / автоответы / журнал | Настройки | Ещё → Настройки → соответствующий раздел | Все прежние CRUD/enable/лимиты/подтверждения; изменения UI не разрешают запись |
| Подключение / баланс Gifts / каталог | Все кнопки в корне Gifts | Подарки → Настроить | Баланс помечен последней проверкой; refresh только по запросу; секреты не показаны |
| Пакетная привязка / разрешение / остановка Gifts | Gifts / карточка | Подарки → Товары; подходящий товар → выдача | Точная категория, способ, поле получения, описание, состав/стоимость, revision и отдельное arm |
| Прогресс Gifts / receipt / возврат / ручное решение | Длинная карточка Gifts | Подарки → История → заказ → История и решение проблемы | Частичная выдача и неизвестность видимы сразу; все прежние платёжные и refund guards без изменений |
| Лимиты Gifts / проверка описания | Корень Gifts | Подарки → Настроить | Верхние защитные пределы, preview/nonce; никаких новых автоматических допусков |
| Продвижение / политика / расписание / лимиты | Большой экран dry-run | Ещё → Продвижение — проверка → вложенные группы | Сохранение локальное, платного исполнителя не добавлено; лимиты и CAS прежние |
| Графики / отчёты / транзакции / прибыль / комиссии | Большое меню статистики | Ещё → Статистика → Графики и отчёты | Исходная финансовая математика и оговорки покрытия; неизвестные суммы не нули |
| Профиль / проверка подключения | Главное меню | Ещё → Профиль | Тот же PlayerokConnection; аккаунт и время; root не доверяет старому OK |
| Настройки уведомлений / безопасности / финансов | Настройки | Ещё → Настройки | Owner/private checks, CAS настроек; права выполнения не меняются |
| История / неопределённые / результаты | Главное меню и диагностика | Ещё → Инструменты → История и результаты | Старые reconcile/resolve guards; никакого слепого повторения |
| Условия / диагностика / sync / backup / cleanup | Разрозненные служебные кнопки | Ещё → Инструменты → Диагностика и данные | Просмотр условий read-only; автопринятие не меняется; backup локальный; cleanup с прежними исключениями и подтверждением |
| Прямые ссылки уведомлений и квитанций | Уведомление → объект | Те же callback → новая карточка | Защищённые сообщения не редактируются; origin сохраняется; результат не переписывается навигацией |

## Реализация и практические ограничения

- Новые модули только UI: HomePanel и presentation. Бизнес-сервисы, workers, схема SQLite, финансовые модели, proxy/MTProto и конфигурация не изменены.
- Публичные названия товаров кэшируются только для подписей кнопок. Пока название не встречалось в чтении товара/заказа, показано «Товар без сохранённого названия», без выдуманного имени.
- Поиск/фильтры сохраняют безопасные параметры отдельно от FSM подтверждений. Возврат в раздел не восстанавливает старые pending/nonce.
- Список диалогов показывает доступный сохранённый фрагмент; непрочитанность берётся только из ответа API. Пока история не загружалась, бот не выдумывает фрагменты.
- Обычная навигация и карточки короткие; формы параметров, подтверждения и подробные отчёты могут быть длиннее ради сохранения функций и существенных предупреждений.
- Просмотр/выбор обычного раздела занимает 1–3 перехода; опасные действия сохраняют дополнительное подтверждение, а редкие инструменты вложены глубже.
- Новые callback только открывают UI; старые callback сохранены. Статические проверки исходников подтверждают, что ни одна исходная функция панели не удалена; это дополняет, но не заменяет Dispatcher-тесты.
- Живое отображение и удобство на устройстве владельца не проверялись. В этой задаче нет реальных отправок, платежей, возвратов или изменений товаров.


## Финальная приёмка — 2026-10-31

- Полный чистый прогон на окончательном коде: **955 passed in 190.97s**.
- Ruff check: **passed**; Ruff format --check: **200 files already formatted**.
- pip check: **No broken requirements found**. Зависимости не менялись.
- Offline selfcheck: **OK**, 110 модулей; временная SQLite проходит версии 1–22; backup/reopen, карточка профиля и график проверены. Внешних запросов: **0**. Новых миграций нет.
- Целевые группы: 124 passed (Gifts + новые UX/диалог), 51 passed (UX/заказы/основные переходы), 17 passed (пагинация и UX на промежуточном шаге). Финальный полный прогон включает все эти сценарии и дополнительные проверки недоступности складской выдачи.
- В tests/test_ux_redesign.py: настоящий Dispatcher, SQLiteStorage и production-панели; меню, вложенные разделы, ошибки ACK, рестарт, сохраняемые фильтры, старый nonce, скрытый запас, защищённое уведомление, условная остановка Gifts, принадлежность владельцу и безопасные доступные действия заказа.
- Существующие сценарии продолжают проверять создание с фото, сохранение FSM, подтверждения/дедупликацию ответов, склад, Fulfillment, пакеты одинаковых подарков, частичную выдачу, потерю платёжного ответа, запрет повторов и возврата после начала оплаты, соглашения, уведомления и сетевые маршруты.
- Ни одна исходная функция/метод панели не удалена (сверка квалифицированных имён с резервной копией). Карта маршрутов и функция → старый экран → новый путь находятся выше.
- Сканирование сигнатур токенов/JWT: 236 tracked/source/document files, **0 находок**; приватные .env / PROJECT_SPEC.md / session / SQLite не отслеживаются Git. Это проверка известных сигнатур, а не обещание абсолютного отсутствия любых секретов.
- Ранние прогоны и корректировки ожиданий старого UI описаны в PROGRESS.md. Проверки бизнес-эффектов и серверных состояний не удалялись ради зелёного результата.

### Что подтверждено локально, а что ещё нет

Все новые UX-сценарии выполнялись на тестовых Telegram/Playerok/MTProto transport и временных SQLite. Сетевые тесты используют контролируемые локальные серверы. Эти результаты **не подтверждают live-доставку**, реальный платёж, возврат, живую работу Worker/SOCKS или удобство на конкретном Telegram-клиенте.

В рамках редизайна реальных покупок, выдач, сообщений покупателям, возвратов и изменений товаров не было. Глобальные разрешения, .env, личная сессия и рабочая SQLite не менялись. Архитектура и публичные бизнес-контракты сохранены.

### Проверка владельцем

Остановите прежний экземпляр через Ctrl+C, запустите start.bat и отправьте /start. Проверьте пять разделов, карточку товара с тремя группами действий, заказ с «Подробнее», диалог с превью ответа и историю подарков. Навигацию проверяйте без подтверждения отправок или платных действий. Для существующей незавершённой операции сначала откройте результат/сверку, не повторяйте её.
