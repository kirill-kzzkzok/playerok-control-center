# Критический аудит Playerok Control Center — 31.10.2026

Исходный срез: **e8ab177**, включая пакеты подарков и консоль,860 тестов.
Один ограниченный adversarial-проход денежных/заказных/сессионных переходов.
Рабочие `.env`, MTProto session и SQLite не изменялись. Реальных покупок,
выдач, возвратов, buyer messages и изменений лотов в аудите **0**.
Количество тестов не является оценкой live-готовности или доказательством доставки.

## Вердикт

**NOT READY** к немедленной платной live-проверке в текущем окружении.

Воспроизведённые P0/P1 ниже исправлены. Технического блокера, требующего переписывать
архитектуру, не обнаружено. Но личная MTProto session отсутствует; credentials уже
заданы локально, без вывода значений. Нельзя подтвердить личность плательщика,
Stars balance, доступность/цену подарка и права выбранного получателя через этот
личный аккаунт. Все write/automation/gift-payment/refund допуски остаются false.

Для перехода к одному контролируемому тесту: локальная авторизация владельцем,
проверка ожидаемой личности и read-only preflight каталога/баланса/получателя,
реальный разрешённый лот и новый PAID заказ, лимит ровно одной согласованной покупки,
другой доверенный получатель (self-recipient кодом запрещён), отдельное разрешение
на Stars, buyer notice и при необходимости SENT/refund. Это не разрешение включать
общую автоматику. Реальный receipt и поведение API должны быть приняты этим тестом.
После исправлений новый общий цикл разработки/аудита не запускается.

## Методика и границы доказательства

- Изучены app.gifts, Outbox/Store, Inventory/Orders/DeliveryChains/Automation,
  OwnerMiddleware/FSM/panels, AccountBinding, SafeAccount/ThreadCalls, startup/shutdown,
  конфигурация/логи и миграции21/22; связанные старые функции проверяются регрессиями.
- Настоящий aiogram Dispatcher, SQLite WAL, production services и TL request/types
  Telethon работают с локальными Telegram/Playerok transports.
- Есть разные SQLite connections/worker claims, held requests, lease expiry,
  cancellation, намеренные malformed responses и настоящие отдельные процессы с
  os._exit сразу после mock server acceptance первого/второго подарка.
- Старые доказанные тесты повторно используются, а новые случаи нацелены на пробелы
  взаимодействия компонентов. Воспроизводящие red XML сохранены, failures не скрыты.
- Безопасный реальный read-only probe использовал существующие SafeAccount/get и
  временную копию DB: **HTTP200/200, CONNECTED, AccountBinding подтверждён**.
  Рабочая DB не открывалась для записи, идентификаторы/username не выводились.
  Артефакт: data/gift_safety_connection_readonly.json. Это только Playerok read proof.

## Подтверждённые находки и исправления

| ID | Приоритет | Причина/файл | Воспроизведение и последствия | Исправление |
|---|---|---|---|---|
| GA-01 | **P0** | app/gifts/service.py:execute | payer меняется после начального identity check во время preflight; вызван SendStarsForm с неожиданным me, receipt затем не совпадает | До intent повторно проверяются текущий payer и pinned personal account ID; неожиданный аккаунт не доходит до оплаты |
| GA-02 | **P1** | app/gifts/service.py:invalid_recipient, app/gifts/execution.py | resolveUsername отверг старое значение, obtaining_fields уже содержит другое; новый допустимый PAID заказ ошибочно возвращён | Исходное значение фиксируется в private gift job; новый read и transport guard требуют то же exact-field значение и правильный item до возврата |
| GA-03 | **P1** | app/security/logging.py:event | handler.emit бросает OSError после claim; исключение выходит из worker до обработки/журналирования результата | Ошибки наблюдаемости не изменяют intent/receipt и не прерывают цикл; event возвращает false без рекурсивного логирования в сломанный sink |
| GA-04 | **P1** | app/services/account_binding.py | DB pinned seller, но native orders.payload.item.user.id указывает другого продавца; binding ошибочно принят | Проверяются и legacy seller.id, и native item.user.id; конфликт не перепривязывает/не очищает DB |
| GA-05 | **P1** | app/gifts/mtproto.py:connect/close/pay | закрытый sender сохранял client и мог reconnect без lock; close во время connect освобождал lock преждевременно | Terminal closed latch, запрет reconnect/pay после close, close сериализован с connect; lock освобождается после disconnect |

Исходные5 red: data/gift_safety_red.xml. После исправления тот же набор5 green:
gift_safety_fixed.xml. Дополнительный red close/connect race:
gift_session_close_race_red.xml, затем green в итоговом наборе.

Тесты GA-01..05: `test_payer_switch_after_preflight_cannot_spend_from_unexpected_account`,
`test_changed_obtaining_username_after_final_rejection_cannot_refund`,
`test_event_handler_failure_cannot_stop_or_quarantine_unattempted_gift`,
`test_binding_checks_native_order_seller_identity`,
`test_personal_sender_cannot_reconnect_after_terminal_close_without_session_lock`.
Owned-client lock и concurrent close/connect проверены отдельно настоящим InstanceLock.

Новая проверка соединения также получила red→green regression медленного запроса:
двойное нажатие, ожидавшее первый долгий get, обходило cooldown от начала запроса.
Теперь cooldown отсчитывается от завершения попытки. Это исправление нового UX,
не отдельная денежная находка. XML: connection_slow_double_red.xml.

**Открытых воспроизведённых P0/P1 в пределах этого прохода не осталось.**

## Новый механизм подключения

PlayerokConnection использует единственный существующий api.call("get"), его
SafeAccount и AccountBinding. В профиле остаётся прежняя PNG-карточка; отдельное
сообщение подключения обновляется той же кнопкой «Проверить соединение».

CONNECTED означает свежий viewer, точный account ID, отсутствие подтверждённой
блокировки и согласованную привязку. SESSION_EXPIRED/HTTP401/blocked — красный;
timeout/network/неоднозначный403 challenge — жёлтый. Raw exception/body не выводятся.
Показываются имя последней успешно проверенной учётной записи и обе даты.
SQLite OK из прошлой сессии не открывает write gate. Lock и5s cooldown защищают
параллельные/двойные/медленные нажатия; «message is not modified» обрабатывается.

Startup всегда делает fresh check. При сбое owner polling/диагностика доступны,
runtime writes/automation false, Outbox не claim-ит операции. Настроенные циклы
ожидают успешного подключения и могут продолжить после явного reconnect без второго
клиента. После известной ошибки transport/auth gate снова закрывается. Любые
дальнейшие API reads, кроме viewer probe, блокируются до проверки, чтобы смена
аккаунта не импортировала чужие сделки/финансы. Final mutation guard проверяет
verification и закреплённый ID непосредственно перед HTTP после throttle.

## Денежные инварианты и fault-сценарии

| Переход | Проверки и доказательство |
|---|---|
| PAID → enrollment | Собственная OUT продажа, item ID, актуальные flags, aware created_at после arm cutoff; уникальный gift_jobs.order_id; duplicate/historical PAID не создаёт новый пакет |
| Лот → gift IDs | Единственная последняя gift: строка; order/position snapshot и description/revision; intentional repeats сохранены; per-component prices и весь пакет фиксированы |
| Получатель | Только exact receiving-field ID, без комментария/профиля/automation fallback; fresh resolveUsername, User и InputPeer; bot/deleted/min/channel/неполный ответ блокируются |
| Изменение получателя | Повторное resolve и pinned User ID для заказа; перед preflight/intent новые серверные deal/item; смена username/owner/лот/отмена блокируют |
| Цена/Stars | Current catalog/checkCanSendGift/user restrictions/form XTR; каждое remaining gift до первого spend; cumulative quantity; сумма пакета и чужие резервы; stale balance fence |
| Intent → pay | SQLite BEGIN IMMEDIATE, CAS/lease/current flags, unique form/reference/position; отдельная Outbox-операция; intent сохранён до RPC |
| Два workers/процесса | Один claim на step; отдельные DB connections не пересекают paid dispatch; InstanceLock запрещает второй runtime/session-file user |
| После RPC ACK потерян | SPENDING → UNCERTAIN, без новой формы/покупки; следующий шаг не регистрируется; no receipt не означает отсутствие платежа |
| Receipt | PaymentResult недостаточен; независимый MessageActionStarGift read, peer/gift/reference/date/payer, отсутствие refunded; неправильный/неполный ответ сохраняет UNKNOWN |
| Частичная доставка | Подтверждённые steps неизменны; remaining BLOCKED/UNKNOWN; нет полного auto-refund/SENT/«всё доставлено»; owner видит список и безопасный pre-intent recheck |
| Рестарт | Истёкший PROCESSING не requeue; после оплаченного RPC subprocess death сохраняет UNKNOWN, включая второй подарок после первого SUCCEEDED |
| Refund | Только окончательный UsernameInvalid/NotOccupied до первой попытки оплаты; свежий тот же receiving field; separate opt-in, native permission/status/owner guard; unique outbox/refund state |
| Refund delay/ACK lost | Независимый get_deal ROLLED_BACK; при задержке UNKNOWN без второй мутации; поздний серверный возврат подтверждается только чтением |
| Final notice/SENT | Только после каждого receipt; одна outcome notice, её ошибка не повторяет gift/refund; существующий legacy Fulfillment с отдельным owner permission; fields/draft ручные |
| Legacy engine | Gift/stock/chain registration взаимно исключены транзакционно; старые Inventory/Orders/reply/auto-reply/finance/products проверки входят в полный pytest |
| Логи/SQLite failure | Секреты маскируются даже DEBUG, raw traceback исключён; handler failure не срывает money state; SQL failure при receipt commit не печатает false success и не разрешает resend |

Подробные тесты: tests/test_gifts.py, test_gift_bundles.py,
test_gift_safety_audit.py, test_playerok_connection.py, test_stock_auto_cycle.py,
test_functional_audit.py, test_console_logging.py и прежние финансовые/transport tests.

## P2 и остаточные ограничения — без реализации

1. **GA-P2-01:** receipt search после потерянного ACK ограничен100 последними
   сообщениями. Вне окна доказательство может остаться недоступным. Результат
   остаётся UNKNOWN; пустой поиск никогда не открывает новую покупку.
2. **GA-P2-02:** неподдерживаемые Telegram объекты/локально неверный формат/отсутствие
   поля уходят в ручное решение, а не автоматический refund. Код не угадывает адресата.
   Playerok receiving field по ранее подтверждённой схеме required=false; приложение
   требует значение, но не может заставить checkout сайта сделать поле обязательным.
3. **GA-P2-03:** незавершённый пакет консервативно удерживает резерв remaining Stars;
   это может снижать доступность других заказов. Автоматическое освобождение/полный
   возврат после частичной выдачи не добавлялись. Fields/draft Fulfillment остаётся ручным.
4. Между последним resolve и Telegram pay нет межсерверной атомарной проверки имени.
   Запрос закреплён за числовым User ID, смена в наблюдаемых проверках блокируется;
   владение username покупателем Playerok доказать нельзя. Покупатель выбирает адресата.
5. Между свежим Playerok PAID и Telegram pay нет распределённой транзакции. Внешняя
   отмена/возврат администратором или самим продавцом в другом клиенте полностью
   не предотвращаются. Не делать внешние refund при неизвестной/частичной выдаче.
6. Файловые locks/бюджеты относятся к одному магазину/DB/session в одном экземпляре.
   Независимые копии DB/session на других машинах и ручные внешние Stars-платежи
   не координируются этой SQLite. Дневной cap — календарные сутки UTC, не rolling24h.
7. Session содержит полномочия личного аккаунта. Git-ignore и lock не защищают
   от вредоносного процесса с правами владельца Windows, утечки backup или копирования
   session. Не публиковать/не синхронизировать private data; использовать OS-защиту.
8. При недоступных log sinks часть событий может не сохраниться. Durable intents/
   receipts в SQLite являются источником состояния. Native вид окна start.bat и
   Ctrl+C с реальным ботом не запускались; launcher contract, shutdown/cancellation,
   настоящие subprocess crashes и restore проверены локально.

## Ответы о безопасности

- **Потерян ответ после Stars:** intent остаётся UNKNOWN, резерв/неопределённый расход
  учитываются; новые покупки закрыты до положительной сверки. Никакой новой формы.
- **Лишний второй подарок:** в проверенных duplicate/lease/restart/concurrent путях
  не отправляется. Два одинаковых подарка в snapshot — ровно две разные позиции.
- **Gift одновременно с auto-refund:** после любого paid intent auto-refund запрещён;
  queue/dispatch повторно проверяют job/order/username. Внешние ручные действия отдельно.
- **Обход лимитов:** воспроизведённые races/restarts/UNKNOWN не обходят whole-order/day
  cap. Другая независимая DB, внешний клиент и доверенность системным часам — границы.
- **Без live нельзя подтвердить:** авторизацию личного MTProto, реальные ограничения
  аккаунта/каталога, списание Stars, форму server receipt, получение адресатом,
  настоящий refund/notice/SENT. Mock не закрывает эти gates.

## Проверки и артефакты

Первый полный срез:890 passed153.79s.38 connection/profile/binding checks,
142 gift/bundle/console/binding compatibility checks,95 selected connection/adversarial/
stock/Dispatcher checks прошли на промежуточных срезах. Ошибка native mock имени
операции item/getItem и недостающий test import были показаны и исправлены.
Окончательный результат pytest/Ruff/selfcheck/миграций/секретов — в итоговой записи
PROGRESS.md и TEST_REPORT.md. XML и safe JSON в ignored data/ доступны локально.

Источники (проверены при аудите):

- https://core.telegram.org/api/payments — повтор того же form_id не повторяет
  транзакцию; приложение всё равно не делает автоматических paid retries.
- https://core.telegram.org/method/contacts.resolveUsername — resolution и окончательные ошибки.
- https://core.telegram.org/constructor/messageActionStarGift — тип серверного evidence.
- Установленный Telethon1.45.0: get_me запрашивает InputUserSelf, request_retries=0,
  auto_reconnect=false; _reconnect не переотправляет pending через новый reconnect.
- Refund updateDeal/ROLLED_BACK контракт ранее подтверждён официальным Playerok UI;
  новая paid/live-приёмка возврата в этом аудите не выполнялась.

## Итоговые проверки

- Полный pytest: **896 passed за153.378s**,0 failures/errors/skips;
  data/gift_safety_full_final.xml. Новые connection/adversarial проверки:36 passed22.39s.
- Ruff check/format:187 файлов чистые; pip check: конфликтов нет.
- Source + изолированно установленный wheel:103 imports, schema22, backup/reopen,
  package resources/PNG OK; external requests0. Новых миграций/зависимостей нет.
- Временная копия рабочей БД18→22: counts/integrity/FK/reopen OK;
  data/gift_safety_migrations.json. Исходная рабочая БД не менялась.
- 228 candidate файлов репозитория: действующих секретов/JWT не найдено.
  Существующий локальный лог не содержит настроенных credentials. Пути секретов
  исключены из Git; runtime write/automation/payment/refund=false.
- Реальный read-only get: HTTP200/200, binding подтверждён на временной копии;
  доказательство не относится к отправкам, покупкам, возвратам или MTProto.

Открытых воспроизведённых P0/P1 нет. P2 и остаточные границы перечислены выше.
Вердикт остаётся **NOT READY** для немедленного платного теста до выполнения
перечисленных owner-local session/read-only preflight/live acceptance условий.
Аудит завершён одним проходом; новый цикл аудита/разработки не начинается.
