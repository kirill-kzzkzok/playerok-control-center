# Исследование Playerok — 2026-10-29

## Зафиксированные источники

- Universal https://github.com/alleexxeeyy/playerok-universal — c797edace06bd5efe3222ff92dc6a623183dfc95.
- PlayerokAPI https://github.com/alleexxeeyy/PlayerokAPI — de0c0854abccbdaa633bffcac1743f6c57517dcf.
- https://playerok.com/ и https://playerok.com/products-sales — реальная старая/новая цена и проценты скидки отображаются публично. Это подтверждает существование скидок, но не протокол установки продавцом.
- https://t.me/s/playerok_merchants?before=64 — официальные сообщения о временных категориях 10%; глобальную постоянную ставку из этого выводить нельзя.
- https://t.me/s/playerok_merchants?before=53 — объявлены изменения стоимости продвижения. Исторические цены нельзя использовать как актуальный quote.

## Аудит исходников

Universal: JSON settings/data без атомарных транзакций; broad except скрывает повреждение,
запас удаляется после отправки, содержимое выдачи логируется. Потоки daemon и
in-memory дедупликация не защищают от перезапуска. Есть автоматический вывод и
автоматическое SENT по событию; исходный runtime не запускается.

API: Account singleton, sync I/O; transport трижды повторяет любые запросы, включая
mutations; cookies разбираются split('=') с потерей части значения. Обнаружена ошибка
multipart updateItem: map указывает attachments вместо addedAttachments.
Парсеры/модели/запросы пригодны для повторного использования с изолированным транспортом.
Зависимости Universal содержат старые requests/urllib3; не переносим весь dependency set.

## Финансы и возможности

fee_multiplier — множитель, не самостоятельная процентная ставка. Подтверждённой
формулы с базовой ставкой и округлением пока нет; точный доход до проверки недоступен.
transactionProviders содержит fee/minFeeAmount/limits, но единицы fee и округление
нуждаются в подтверждении. Получение этих данных не является выводом средств.
get_item_priority_statuses(item_id, item_price) получает price/type/period без покупки.
publish_item и increase_item_priority_status могут списывать деньги.
create_item создаёт серверный черновик, публикация — отдельная операция.
PAID/PENDING/SENT/CONFIRMED/CONFIRMED_AUTOMATICALLY/ROLLED_BACK — разные статусы.
update_deal(SENT) отмечает выполнение продавцом, не подтверждение покупателем.
В create/updateItem библиотеки нет подтверждённого поля установки скидки.

## Пока не подтверждено live

Авторизация, действительность persisted hashes, реальные категории и обязательные поля,
размеры/лимиты фото, актуальные комиссии, бесплатность тестовой публикации,
стоимость PREMIUM конкретного товара, аватар, история. Проверки будут дополнены.


## Проверено live 2026-10-29 (обновление)

Сессия авторизована, профиль доступен; games/items/deals/chats читаются.
Steam: 10 категорий; реальные SELECTOR/RANGE и INPUT/TEXTAREA. Для категории
«Другое» список obtainingTypes пуст и dataFields пуст: мастер должен пропускать
несуществующий способ получения, а не показывать фиктивный вариант.

OUT providers фактически вернули: SBP fee=6 minFeeAmount=60; BANK_CARD_RU 6/60;
USDT 4/null; BANK_CARD 10/150; YMONEY 6/60. Это сырые параметры, не готовая
гарантированная схема расчёта. LOCAL не является внешним способом вывода.
Лимиты и актуальная дата сохранены локально в data/live_market_metadata.json.
Для одного существующего лота read-only quote вернул PREMIUM=13 ₽, период 30 дней,
диапазон цены 90–500. Это пример конкретного текущего quote, не глобальный тариф.
Обычный бесплатный статус в этом ответе отсутствовал: предполагать DEFAULT=0 нельзя.

Официальный публичный frontend, модуль 96644: income=floor(price*(1-feeMultiplier))
в целых рублях. Значение feeMultiplier здесь используется прямо как доля (0.1/0.2),
а не умножается на выдуманную базовую комиссию. Добавлен отдельный адаптер оценки
sale_rule_from_api с происхождением данных. Фактические расчёты транзакций ещё не сверены.
Глобальная константа 0.2 в UI не подставляется при отсутствии данных категории.

Публичные JS-файлы получены обычным GET без cookies и обхода защиты.
Ниже ссылки и SHA256 файлов, содержащих использованные формулы:

- https://playerok.com/_next/static/chunks/5122-2a0a62ac930ce12f.js — SHA256 0b994352204eb54278e5b8dd5f4a94c9842d7b32fc219e9bfe5e5d1c0d1dbeb9

- https://playerok.com/_next/static/chunks/9302-2b4d731f0be1a067.js — SHA256 b93b2446ca23fa02a758f936ca0a5e3c68ca417fb648f1537a51652010128c87

- https://playerok.com/_next/static/chunks/6431-a9e2c7d6ff44d24b.js — SHA256 4a6f3b615cefb4805932c073c8838df9820a24704e8a9431003349e6f4005e17


Контролируемый createItem с явной технической маркировкой отклонён HTTP 400,
BAD_REQUEST: введённые данные товара должны соответствовать правилам. После запроса
проверены все доступные собственные лоты; объект по уникальному имени не найден.
Технические объявления повторно автоматически не создаются; обход правил не применяется.
Публикация и редактирование реального тестового лота остаются неподтверждёнными.


## Дополнение к контракту создания и комиссий (2026-10-29)

Официальные загруженные сегодня public bundles 25.js/26.js:
- Когда нет структурированных данных ITEM_DATA, форма требует textData и отправляет
  его в createItem.input.comment. Это приватные данные покупателю, не description и
  не attributes. Добавлен optional comment в vendored Account.create_item и обязательное
  приватное поле в динамический мастер. Проверено test_category_contracts.py и реальным
  aiogram pipeline на mock transport. На сервер повторное создание не отправлялось.
- Модули 66572/85570: textData обязателен, 2–2048 UTF-16 units; name 10–72,
  description 10–2048. Обычная цена 90–100000 RUB; роль OFFICIAL_SELLER имеет минимум 10.
  Эти frontend ограничения не подменяют финальную серверную проверку категории/контента.
- Модель eR выбранного obtaining использует obtaining.feeMultiplier ?? category.feeMultiplier.
  Начальный обзор категорий может показывать минимальную ставку, но после выбора способа
  применяется его ставка. Исправлена потеря category fallback; глобальные 20% не подставляются.
- Библиотечный updateItem multipart mapping подтверждён отдельным тестом HTTP payload.
  Замена фотографий рабочего товара live не выполнялась.

Источники с хешами локально прочитанного контента:

- https://playerok.com/_next/static/chunks/pages/_app-81aa885e88a4fc68.js — SHA256 f0f6277b94003778e49aa08f6d5fccaa0fde9b2eece35991ceb6e90d58e701ea

- https://playerok.com/_next/static/chunks/9092-ff2d2cafe217731b.js — SHA256 c911260ade34aad6b1b0abeb2978daf63ad92370285f862e2709b86d7a29b9bd

- https://playerok.com/_next/static/chunks/pages/sell-c39653030b4e944c.js — SHA256 be7a8c7ceda6b4ece13ea456e2341dd46b3a8eabf03cf6e9056d23e4605bea01


## Редактор товара и вывод: новые официальные источники, 2026-10-29

Из публичного build manifest получены дополнительные bundles редактора товара и
страницы вывода. Загружены обычным GET без cookies, 10/10 HTTP 200.

Подтверждено в официальном редакторе (4354c395451c.js в локальном исследовании):
- UpdateItemInput включает comment; при непустом comment форма передаёт dataFields=[].
- Для обычного продавца исходная rawPrice сохраняется. Поле price — фактическая цена
  продажи, в том числе со скидкой. При пустой цене со скидкой форма отправляет rawPrice.
- Верхняя граница скидочной цены — floor(rawPrice*.95), нижняя — минимум для роли;
  при недостаточном диапазоне UI скрывает ввод скидки. Изменение rawPrice доступно
  лишь специальным ролям; наш адаптер его не добавляет.
- Сценарий скидки в боте реализован с отдельным подтверждением и проверкой свежих
  price/rawPrice. Проверен на mock; live-изменение рабочей цены не выполнялось.

Подтверждено в коде вывода (587921dc4784.js / abc07b5139a5.js):
- fee задаётся в процентах; базовый расчёт fee_amount=amount*fee/100.
- Дополнительный сбор USDT — 1 USDT, в RUB используется текущий exchangeRate.
- Применяется minFeeAmount: max(base_fee+extra_fee,minFeeAmount).
- Историческая ветка для транзакций до 2025-04-01 отдельная: современные ставки
  нельзя переносить на старые операции. Округление фактического расчёта пока не сверено.

Обычная read-only introspection input types вернула HTTP 500. Не обходили ограничения
и не повторяли запрос; контракт update/comment/discount подтверждается public frontend,
а не неуспешной интроспекцией. Безопасное чтение условий аккаунта прошло, mutations=0.

Дополнительно в ранее полученном официальном общем bundle подтверждены
acceptGameCategoryAgreement($input: AcceptGameCategoryAgreementInput!) и входные
agreementId/userId. Read gameCategoryAgreements с userId возвращает непринятые условия;
соглашения получения используют тот же механизм принятия. Реальное принятие не запускалось.

Новые источники с хешами:

- https://playerok.com/_next/static/chunks/6476-589a32cc120d2139.js — SHA256 af96a5e8192b40f48e11c9dcd2cc89709fd28a2bd2559ea4c79cf23e74ad6977

- https://playerok.com/_next/static/chunks/pages/wallet/withdraw-afd5dceb017e6632.js — SHA256 f52f1c1a0bba0b6ebe96a0c04e7bc596f43afbf57956702bfccbd8cf87c4f371

- https://playerok.com/_next/static/chunks/pages/products/[slug]/edit-4767bcce2b41850b.js — SHA256 cd33d827c17c764e35bca13fcd910fbb0bf53ee768c27acb58f0482aa3f53c05


## Подтверждение exchange rate и расчётов вывода (2026-10-29)

В официальном общем JS найден read-only запрос:
`query getExchangeRates($pair: ExchangePair!) { getExchangeRates(pair: $pair) }`,
enum USDT_RUB. Wallet UI обновляет курс примерно раз в 60 секунд. Этот точный контракт
добавлен в безопасный адаптер и успешно проверен GET-запросом на реальном API.

Read-only исследование получило 5 OUT providers и курс USDT/RUB; построены оценки для
явно условных 2000 RUB без вывода/покупки/изменений. Свидетельство —
`data/live_withdrawal_quotes.json`, mutations=0. Настоящий settlement не выполнялся,
поэтому правила округления не объявлены точными. Неизвестные ставки/лимиты/курс
не заменяются фиксированными значениями. Минимальная комиссия применяется после доплаты.
Для USDT отображается рублёвый эквивалент, а не выдуманное количество выплаченных токенов.

Public item UI запрашивает itemPriorityStatuses с price=item.rawPrice. Исправлено
использование скидочной цены при quote размещения; обе цены проверяются перед
бесплатной публикацией, отсутствие rawPrice блокирует предположение стоимости.
Все проверки записи публикации пока изолированные; live-публикация не запускалась.
Источники: ранее зафиксированные официальные bundles 4.js, 23.js,
587921dc4784.js и abc07b5139a5.js с URL/SHA256 выше.


## Chat photo contract recheck — 2026-10-30 MSK

Current [official application bundle](https://playerok.com/_next/static/chunks/pages/_app-81aa885e88a4fc68.js) fetched without credentials, HTTP 200.
Observed uploadChatImageIntoTemporaryStore(file: Upload!, input: UploadTemporaryAttachmentInput!)
returns expiresAt/id/url/chatId/clientAttachmentId. The chat bridge passes chatId/text/imagesIds
to createChatMessage. No remote code was executed. Source/metadata: .vendor/public_web/chat-current.js
and data/chat_image_contract_check.json. Current raw SHA256:
6c3fb66e2c445e36b6021767b7baeba5963f041ce03f7bc43cc5398b5c2452bb.
Raw bytes differ from the older snapshot; the fields used here were inspected again.
This confirms published client usage, not live successful sending or idempotency.
No authenticated temporary uploads, buyer messages, withdrawals or paid operations performed.


## WebSocket — 2026-10-30 MSK

The current [official application bundle](https://playerok.com/_next/static/chunks/pages/_app-81aa885e88a4fc68.js) contains
wss://ws.playerok.com/graphql, graphql-transport-ws, connectionParams x-gql-op/path/timezone
and chatUpdated/chatMarkedAsRead/userUpdated subscriptions. PartialUserProfile on
UserProfile spreads User and UserFragment fragments: direct id on the union is invalid.
Server validation independently confirmed that (data/ws_schema_probe.json). Minimal
inline-fragment subscriptions then stayed connected for 45 seconds and received JSON
pong (data/ws_heartbeat_fixed_probe.json). No buyer event delivery was verified,
no messages or mutations were sent. Source inspected lexically, never executed.


## Seller fulfillment — 2026-10-30 MSK

Public route metadata: data/public_chat_routes.json and data/public_deal_routes.json
(18+11 unauthenticated GETs; URLs/SHA recorded there). The /deal/[id] code in
.vendor/deal_routes/6aea091dcf56.js has separate buyer/seller controls: seller PAID
uses SENT, buyer SENT uses CONFIRMED subject to confirmationRestriction. The seller
flow calls GET /deals/{dealId}/fulfillment-report/requirement then may submit checklist
or fields to /deals/{dealId}/fulfillment-report. Its legacy fallback is not treated
as permission for this project to bypass an unknown requirement. No such writes run.
Relevant source URL is the /deal/[id] chunk listed in public_deal_routes.json.
Native PlayerokAPI get_deal parameter is deal_id; passing id was a local call-site bug.
It is corrected and verified against real read-only API and mock native transport.

[Official deal page bundle](https://playerok.com/_next/static/chunks/pages/deal/[id]-44549e1716e305e1.js); SHA256 1c7f7d36dde139dc6866a4828aac6ba6e604fcfdea8a3e5b25af8b9351d86206.
