# Final UX acceptance — 2026-11-01

## Scope and evidence

Production views were exercised through the real aiogram Dispatcher/FSM, a temporary SQLite and controlled Telegram/Playerok transports. No live messages, payments, listing mutations, production database or session were used. This is offline acceptance, not a Telegram-client visual certification.

Original screenshots mentioned in the attachment were not supplied as files in this turn. The BEFORE render uses the trusted historical source at `9f5dee1` over the same mock fixture, not a fabricated screenshot.

## Complete screen/module inventory

Every file in `app/telegram/` is listed. Infrastructure rows have no independent screen. Existing service checks, callback routes and confirmations are retained.

| Module | Old screen / function | Current structure / purpose | Entry / routes |
|---|---|---|---|
| `__init__.py` | Пакет | Без пользовательского экрана | `—` |
| `agreements_panel.py` | Условия продавца | Необязательный просмотр; действующие автоматические полномочия не изменены | `agv:*` |
| `auto_replies_panel.py` | Автоответы, правила и журнал | Сводка → мои правила / настройки / история; без скрытого включения | `ar:*` |
| `automation_panel.py` | Автовыдача и проверки | Основное → дополнительные параметры; все причины доступны; без отправки при чтении | `automation; auto:*; ap:*` |
| `automation_queue.py` | Цепочки / очереди / расходы / расписания | Служебные подробности внутри инструментов; сверка не означает повтор | `aq:*` |
| `callback_ack.py` | Подтверждение callback | Отдельно устаревание, сеть и действие; никаких денежных повторов | `Общий middleware` |
| `chat_panel.py` | Переписка / страницы / фотографии | Имя из проверенного списка, время МСК, хронология и вложения | `chat:*; chatpage:*; chatmedia:*` |
| `drafts_panel.py` | Сохранённые черновики | Список → продолжить / отменить / серверный результат; восстановление FSM | `drafts; draft:*; dl:*` |
| `finance_panel.py` | Расчёт товара | Цена → комиссии и расходы; не обещает историческую прибыль | `finance:*` |
| `formatting.py` | HTML и длинные тексты | Экранирование, UTF-16 лимиты и безопасные страницы | `Без самостоятельных callback` |
| `fulfillment_panel.py` | Подтверждение выполнения | Фактическая передача → проверка продавца → допустимая форма / отчёт | `ff:*` |
| `gifts_panel.py` | Подарки: подключения, каталог, лоты, пакеты, история, сверка | Короткие сводки; пакет по позициям; риски и стоимость в явном согласии | `gft:*` |
| `home_panel.py` | Главная / Ещё / Инструменты | Пять постоянных пунктов, внимание только по данным | `home; more; tools:*; attention` |
| `interface_cleanup_panel.py` | Очистка интерфейса | Предпросмотр → подтверждение; квитанции / пользовательские сообщения сохраняются | `ic:*` |
| `item_category_picker.py` | Выбор категории | Иерархия с поиском и постраничным выбором | `icp:*` |
| `item_panel.py` | Карточка / редактирование / выдача / прочее / детали / фото | Короткая карточка; редкие действия вложены; удаление с подтверждением | `item:*; iv:*; itemmedia:*` |
| `keyboards.py` | Общие клавиатуры | Пять основных пунктов; контекстные возвраты | `main_menu / keyboard` |
| `launch_panel.py` | Настройка до публикации | Черновик → готовность → ограниченное подтверждение запуска | `launch:*; ln:*` |
| `listings_panel.py` | Товары / чаты | Товары с ценой; Chat.users / last_message, без UUID | `items; chats; listnext:*; il:*` |
| `message_notice.py` | Переход из уведомлений | Сохраняет origin и безопасное подтверждение ответа | `Служебные deep links` |
| `operation_results.py` | Квитанции / результаты действий | Очередь, неизвестность и подтверждённый успех различаются; доказательства сохраняются | `result:*` |
| `operations_panel.py` | Операции и ручная сверка | Список с русскими именами без ID; ID и коды остаются в карточке диагностики | `ops:*` |
| `order_finance_panel.py` | Учёт заказа | Себестоимость / расходы / сверенные продажи / проверка | `pf:*` |
| `orders_panel.py` | Заказы, карточка и история | Краткая карточка → история / подробности; оригиналы в диагностике | `orders; order:*; od:*; delivered:*` |
| `paid_promotions_panel.py` | PREMIUM и тарифы | Тариф → конкретная сумма и подтверждение; отдельное платное расписание | `payp:*` |
| `panel_tracking.py` | Редактирование экранов | Сохраняет квитанции / уведомления; fallback при невозможности редактирования | `Общий transport` |
| `presentation.py` | Единый словарь и форматирование | Контекстные статусы, время МСК, названия операций и имена собеседников | `Без самостоятельных callback` |
| `profile_panel.py` | Профиль и соединение | Новый read-only запрос по кнопке; свежесть и привязка аккаунта | `profile; profile:check; profile:details` |
| `promotions_panel.py` | Расписание, бюджеты и dry-run | Короткая проверка → параметры; отдельное разрешение платного расписания | `promotions; promo:*; pp:*; pi:*` |
| `replies_panel.py` | Быстрые ответы | Список → шаблон / ввод → предпросмотр с подтверждением отправки | `quickreplies; quick:*; reply:*` |
| `router.py` | Dispatcher / FSM dispatch | Существующие маршруты сохранены; новые ветви остаются в панелях | `Центральная маршрутизация` |
| `settings_panel.py` | Уведомления, подключения, безопасность | Список настроек; переключатели не меняют разрешения платных действий | `settings; sp:*` |
| `statistics_panel.py` | Статистика и 13 видов отчётов | Сводка → графики / лучшие товары / финансы / подробности | `stats:*; sr:*; st:*; sd:*` |
| `stock_panel.py` | Склад и перенос запаса | Счётчики без содержимого → загрузка / подтверждение / явное возобновление | `stock:*` |
| `storage.py` | Устойчивое состояние FSM | SQLite FSM; без изменения схемы и гарантий изоляции | `Инфраструктура` |
| `transport.py` | Telegram transport | Существующий SOCKS/Worker; без direct fallback | `Инфраструктура` |
| `wizard.py` | Мастер создания и редактирования | Категория → поля → фото → выдача → серверный черновик → запуск | `create; w:*` |

## Changed screens and access paths

- Главная → Заказы → карточка → История: no order UUID / raw statuses / policy version on summary; direct history button. Seller-marked sending remains distinct from buyer-confirmed receipt.
- Заказы → Подробнее: full order ID, operations, local note/problem, costs, stock and safe fulfillment remain available.
- Главная → Сообщения: participant selected from actual API `Chat.users` excluding the shop; `last_message` preview, unread count and Moscow time. Unknown identity is neutral; no per-peer API fan-out.
- Диалог: chronological messages, author, Moscow time, photo viewer, pagination/nonces, compose and quick replies unchanged. Existing cached peer name is used when available.
- Ещё → Статистика: revenue / paid count / average / uncomputed profit honesty. Charts, rankings, transactions, profit and commissions grouped into small menus. Historical `sr:days`, `st:*` and `sd:*` remain working.
- Настройки → Автоответы: count summary, create, rule list, settings and history. Per-rule limits and conditions moved behind details; actual disabled permissions remain visible. Enabling still needs the old explicit future-message confirmation.
- Инструменты → История операций: Russian operation/status labels, no shortened UUID; exact ID and reconciliation still in operation details.
- Gifts and seller-fulfillment consent texts explain the allowed spending/completion/refund boundaries in Russian. No payment or automatic-completion control was enabled by this change.

## Preserved safeguards

Owner/group isolation, paid consent, nonces, SQLite FSM persistence, Outbox idempotency, unknown-result blocking, receipts, irreversible-delivery warnings, stock secrecy and network routing are unchanged. Viewing these screens queues no mutation. `SENT` is rendered as «Продавец отметил отправку»; it never claims buyer confirmation.

## Offline visual artifacts

Reproduce: PowerShell `$env:PCC_RENDER_OUTPUT="data/acceptance_ux"`; `.venv/Scripts/python -m pytest tests/test_takeover_ux.py::test_render_production_screen_cards_offline -q`.

The test saves actual production text and inline keyboards as 13 PNG cards and an HTML gallery under `data/acceptance_ux/index.html`, plus 3 historical BEFORE cards. Each image is explicitly watermarked OFFLINE / TEST TRANSPORT / НЕ TELEGRAM. The artifacts stay local in ignored data, never include real secrets or chats.

| Screen | Artifact | Visual review |
|---|---|---|
| 01_home | `data/acceptance_ux/01_home.png` | Offline card; actual production text / keyboard |
| 02_order | `data/acceptance_ux/02_order.png` | Offline card; actual production text / keyboard |
| 03_order_history | `data/acceptance_ux/03_order_history.png` | Offline card; actual production text / keyboard |
| 04_statistics | `data/acceptance_ux/04_statistics.png` | Offline card; actual production text / keyboard |
| 05_autoreplies | `data/acceptance_ux/05_autoreplies.png` | Offline card; actual production text / keyboard |
| 06_chats | `data/acceptance_ux/06_chats.png` | Offline card; actual production text / keyboard |
| 07_conversation | `data/acceptance_ux/07_conversation.png` | Offline card; actual production text / keyboard |
| 08_product | `data/acceptance_ux/08_product.png` | Offline card; actual production text / keyboard |
| 09_automation | `data/acceptance_ux/09_automation.png` | Offline card; actual production text / keyboard |
| 10_gifts | `data/acceptance_ux/10_gifts.png` | Offline card; actual production text / keyboard |
| 11_premium | `data/acceptance_ux/11_premium.png` | Offline card; actual production text / keyboard |
| 12_operations | `data/acceptance_ux/12_operations.png` | Offline card; actual production text / keyboard |
| 13_delivery_result | `data/acceptance_ux/13_delivery_result.png` | Offline card; actual production text / keyboard |

## Findings / limitations

- Confirmed UI defect: real `Chat.users` and `last_message` ignored; UUID shown as peer label. RED test failed on `private-chat-uuid`; fixed without extra network calls.
- Confirmed usability defects: rule menu exposed runtime/regex prose; order history exposed LOCAL_NOTE / status transitions; report menu had no grouping. RED Dispatcher tests failed first, then passed through the redesigned transitions.
- No real Telegram client was controlled; font, emoji and line wrapping in the actual client still require a manual pass. Offline renders do not prove Bot API delivery.
- Less frequent advanced screens remain intentionally detailed: budgets, reconciliation evidence, report fields and diagnostics. Some historical services return diagnostic codes which these dedicated screens preserve alongside explanation; this work does not rewrite backend state labels.
- Chat search is not invented: no reliable server username search contract is available in the current API client. Pagination and cached-history preview are preserved.
- Summary profit is not extrapolated from sale price or current product cost; full reviewed-cost report remains under Финансы.

## Verification

- RED: `data/takeover-ux-red.txt` (four failures); corrected actual-page chat RED `data/takeover-chat-red.txt` (UUID fallback failure).
- Operations RED: `data/takeover-operations-red.txt` (raw kind/status/short-ID).
- GREEN: UI / chat / statistics / auto-replies / order-management / reconciliation suite: **125 passed**, exit 0, 30.87s (`data/takeover-ux-all.txt`).
- Offline 13 screens + 3 BEFORE: **1 passed**, exit 0, 14.58s (`data/takeover-render-final.txt`).
- Full project testing and live acceptance are recorded by the root task in FINAL_ACCEPTANCE_REPORT.md; no local UI test is claimed as live proof.

- Additional Gifts/Fulfillment UI regression: **98 passed**, exit 0, 29.58s (`data/takeover-gifts-ux.txt`). Final diagnostic-history/peer fallback check: **31 passed**, exit 0, 9.82s (`data/takeover-ux-final-pass.txt`).
