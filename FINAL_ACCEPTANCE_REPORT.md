

## Independent User Journey QA — isolated acceptance harness

Date: 2026-11-01. Scope: actual production Dispatcher/FSM, SQLite WAL, wizard, ListingLaunch, Synchronizer, GiftWorker, OutboxWorker/Executor, PersonalGiftSender and permitted Fulfillment; every external response is controlled in-process. No working .env, SQLite, Telegram session, Playerok mutation, buyer message or Stars payment was used by these tests.

Implementation: `tests/test_final_acceptance_e2e.py`, reusable `AcceptanceHarness` / `AcceptancePlayerok`. The local fake has no network-forwarding fallback: unimplemented calls fail. The sender uses real TL requests against a strict fake MTProto transport; spending and independent history reads remain production logic. Test-owner permissions and 15 Stars order/day authority are explicit and isolated.

### Proven complete mock journey

1. `/start` and create through the real aiogram Dispatcher: game, category, gift obtaining type, hidden seller data, product name, final `gift: heart` description, image sent as a document, price, gift delivery, final creation confirmation.
2. Real Outbox creates one server DRAFT; validated readback binds its ID. It does not publish.
3. Launch editor sets the exact post-delivery message `Все отправлено, спасибо за покупку!`, permits eligible automatic completion, and validates current gift/cost/balance/schema. True close/reopen of the WAL database preserves the preview/FSM.
4. Final confirmation atomically saves the gift binding/revision/armed timestamp, launch marker and one publication outbox row. The fake publication transport asserts delivery was already enabled before the mutation.
5. Publication returns APPROVED and introduces the first immediate PAID sale only in the fake remote state. No direct Orders.ingest is used: the real Synchronizer must discover it. The exact obtaining field is `@kzzkzok`; Playerok profile/comment contain deliberately different values.
6. Real GiftWorker registers one job/step/outbox operation. Repeat synchronization does not register a second intent.
7. Real PersonalGiftSender resolves the exact recipient, checks catalog/restrictions/balance/form and sends one mock 15 Stars payment; independent get_messages receipt checks User ID, gift ID, reference and message ID.
8. Only after confirmed receipts do real follow-up workers send exactly one message to the mock Playerok chat and perform the permitted seller SENT operation. SENT is not buyer confirmation. The completion outcome is checked in its linked Outbox operation and independent server status; gift_jobs.completion_state=QUEUED describes registration, not delivery success.
9. Reopening SQLite and repeating sync/workers three times causes no additional creation, publication, payment, notice or completion. Default promotion evaluation creates no paid promotion.

### Fault evidence

- Twelve full production journeys mutate one boundary after enrollment: unpaid/cancelled order, missing username (even when present in comment), wrong field ID, username reassignment, gift disappearance, price change, insufficient balance, recipient gift restriction, changed description, owner stop, resolve timeout. All stop before a payment intent; no success notice, completion or refund is emitted.
- Six full journeys exercise one actual mock payment followed by lost ACK or absent/mismatching/refunded independent receipt (recipient/gift/reference). Each preserves SPENDING evidence as UNCERTAIN, sends no success notice, and does not buy/refund/complete again after database restart and repeated workers.
- Cancellation immediately after durable SPENDING and before the transport: no paid RPC occurs; uncertainty is retained conservatively and no automatic retry is admitted.
- Independent receipt read followed by injected SQLite finish failure: database remains SPENDING; expired-lease recovery becomes UNCERTAIN; explicit read-only reconciliation confirms the receipt without another payment. Only then are notice and completion allowed.
- Two remote paid sales, repeated synchronization and concurrent workers under 15 Stars total authority: exactly one spends, the other is BLOCKED; restart does not exceed the authority. The winning order is deliberately not assumed in the fake transport.

### Actual commands and limits

`python -m pytest tests/test_final_acceptance_e2e.py -q --tb=short`: **22 passed in 20.12s, exit 0** (local artifact `data/final_acceptance_e2e.txt`, ignored). Ruff check/format for the test: exit 0. Earlier harness failures were corrected invalid page_info fixtures, a mistaken expectation of a nonexistent completion-state enum, and a fake that assumed the first order always won the concurrent race. None is represented as a production bug.

This is mock E2E evidence, not live Telegram delivery, real Playerok sale, paid PREMIUM acceptance, or visual verification in a Telegram client. Other reviewers and the main engineer own the separate financial/concurrency audit, UI renders and live acceptance sections.


## Independent adversarial follow-up — runtime and entry points

Reviewer: User Journey QA role, separate from the financial/concurrency implementation. Bounded second assignment within the same takeover pass; no live operations performed. Parsed all 115 production Python modules with AST to enumerate direct mutation/send sites, then traced the listed critical services. This syntax/call-site inventory is not represented as full semantic review of every line.

| Entry point | Admission and local commit | External effect | Success evidence | Failure/restart boundary |
|---|---|---|---|---|
| Wizard submit → Products.queue_create | Owner/FSM revision, current schema/agreements, atomic DRAFT CAS + unique create key | Executor createItem under SafeAccount permit | Returned DRAFT ID, seller/category/fields/photos checked, persisted binding | Missing/mismatching response is unknown; no blind create repeat |
| Launch final button → ListingLaunch.queue | Fresh item/account/quote/config; one BEGIN IMMEDIATE for delivery, grants, reserve, marker and queued draft | Existing publish/premium_publish Outbox | Independent item readback; premium additionally exact new LOCAL confirmed expense receipt | Proven local failure release is CAS; sent/unknown outcomes retain authority and are not requeued |
| Synchronizer → Orders.ingest | Valid page envelopes, OUT deals, deduplicated inbox/order updates; terminal status does not regress | Reads only | API pages and durable cursor checkpoints | Repeat imports deduplicate; read errors preserve checkpoints/backoff |
| GiftWorker → GiftService.discover | PAID/current seller/item, future cutoff, enabled immutable bundle snapshot, no Inventory/chain/fulfill conflict | Registers one gift_send intent per order/position | Unique jobs/steps/outbox; no payment at discovery | Old PAID repeats cannot register another intent |
| Outbox gift_send → GiftService.execute | Current order/listing/lease/runtime; exact obtaining field; uncached recipient resolve twice; current catalog/form/balance; atomic SPENDING and budget reservation | PersonalGiftSender.pay → SendStarsForm | Separate get_messages receipt checks peer/gift/reference/date/message ID and no refund | Lost ACK, cancellation after intent or lost receipt COMMIT → UNCERTAIN; no automatic new form/payment |
| Gift reconcile | Owner, one unknown spent step, stored payer/recipient/gift/reference | Independent Telegram read only | Exactly one matching receipt | Empty/mismatched search never permits buying again |
| GiftWorker follow-ups | Every gift step confirmed; runtime and per-listing completion permission; current fulfillment requirement | gift_notice or existing fulfill Outbox | Notice returns message ID; fulfill independently confirms server result | Notice failure does not repeat gift; report-required deals remain manual |
| Definitive invalid recipient → gift_refund | Only classified final resolve errors, fresh PAID owned deal, capability, no started gift intent; one unique refund operation | Protected updateDeal ROLLED_BACK | Fresh get_deal ROLLED_BACK | Network/FloodWait is not invalid username; unknown/refunded/sent states prohibit another gift/refund |
| Inventory → Orders.reserve / DeliveryChains.create | Owned item, PAID and exact current snapshot, unique inventory reservation/order chain, mutually exclusive Gifts | Existing delivery/chain_message Outbox | Confirmed message receipt and atomic inventory DELIVERED transition | Failed/unknown stock quarantined; refill does not resume owner stop |
| Notifications / OperationResults | Owner-scoped one-attempt event registration; baseline/suppression; critical unknown alerts retained | Telegram Bot API only through configured session | Telegram message/edit response stored | Unknown Telegram sends not repeated automatically; original delivery receipts retained |
| Local personal authorization | Existing session lock and localVPN-owned transport | Telethon login/read session | Authorized personal User; runtime binds payer ID independently | No session replacement or paid RPC from login path; no bot-account spending |
| Bot API / localVPN | One route, local SOCKS ownership/ready check and generation; redirects rejected | Existing managed service + aiogram requests | Actual transport responses, never TCP-only readiness | No direct fallback; route loss blocks new admission, does not rearm paid intents |

### Adversarial findings and boundaries

- Confirmed new UX blocker introduced by preserving the emergency stop: a fresh stopped store had no independent owner resume route. Only arming an already APPROVED listing reset the flag, so the first prepublication gift launch had no usable path. Two Dispatcher RED tests are in `tests/test_final_acceptance_e2e.py`; artifact `data/final_acceptance_resume_red.txt`, 2 failed, exit 1. Main engineer owns the explicit resume/CAS fix. A second test injects emergency STOP during the resume catalog await.
- Confirmed counterexample to an unconditional first-sale guarantee: with a second-precision remote created_at in the same second as fractional local armed_at, GiftService.discover skips and EnrollmentWorker marks the immediate sale skipped. Real ListingLaunch → publication → real discovery tests reproduced both (artifact `data/final_acceptance_timestamp_red.txt`, 2 failed, exit 1). This does not prove that the live Playerok API currently truncates these timestamps; actual server precision/clock evidence is a separate requirement. Weakening every future-order cutoff would risk admitting older orders and was not performed by this reviewer. Final disposition belongs in the main report.
- Gift launch readiness does not currently version the global gift limit setting across all PREPARE awaits. A later tighter limit is still enforced by the real payment guard, preventing overspend, but may make a previously displayed readiness stale. Treat as a consistency limitation, not evidence of successful ready-to-pay publication.
- No new confirmed payment replay, receipt substitution or post-payment refund defect was demonstrated in this bounded runtime review. This is constrained negative evidence, not an absolute safety guarantee; real server behavior, MTProto routing and receipt details still require the separately authorized live test.

Reviewed runtime paths include Inventory/Orders/Automation/DeliveryChains, enrollment/sync/Outbox, gift core/settings/service/worker/execution/outcomes/sender, account verification, access/instance/logging, personal auth, Bot API transport/localVPN, and SQLite transaction/migration/lease recovery. Migrations 021/022 preserve old gift IDs, reference/form/receipt and statuses; no migration is added by this QA work. Documentation reviewed in chunks includes README_GIFTS, architecture contracts, historical testing conclusions and the recorded 2026-10-30 creation/discount/free-publication/deletion evidence. That historical Playerok test proves neither premium spending nor real gift delivery.

## Main engineer: confirmed defects and disposition (2026-11-01)

| Finding | Evidence | Disposition |
|---|---|---|
| F1 missing PREMIUM delivery revision | Real ListingLaunch.queue payload RED | Fixed payload; matching-revision release |
| F2 STOP proof lost marker before reading it | Real queue + reconciliation RED | Shared atomic Store.release_launch reads marker first |
| F3 old release disables newer owner's delivery flag | Actual Automation.save + old failure RED | Flag changes only after policy CAS succeeds |
| F4 PREPARE enables global schedule | Preview/enqueue fault injection RED | Schedule change moved into atomic COMMIT; observations explicitly distinguished |
| F5 launch/arm cancels global gift STOP | Queue and published-lot arm RED | Explicit resume + stop generation CAS; lot arm cannot resume shop |
| F6 optional username schema blocks valid setup | True read-only Playerok field OBTAINING_DATA/required=false | Setup admitted; exact mandatory deal value and resolve still gate spending |
| F8 launch bypasses aggregated PREMIUM budget/reservations | Reserved/succeeded/uncertain expense and changed-policy RED | Atomically rechecks budgets/reservations/policy before enqueue |
| F8 launch A authorises paid promotion of B | Actual PromotionWorker→Outbox mock paid RPC RED | Per-item scoped launch grant; global arm remains separate |
| Conflicting gift/text policies | Real bind/arm then text launch RED | COMMIT rejects enabled competing mode |
| Concurrent delivery STOP overwritten by preparation | API await interleaving RED | Read-only revision/flag token compared inside COMMIT |
| First-sale clock precision | Production discovery RED for coarse server date | Narrow fresh-create provenance cutoff; generic old drafts stay strict |
| Wrong chat identity/UUID fallback | Dispatcher on real Chat.users envelope RED | Excludes own account, uses actual peer; neutral missing-name fallback |

P2/limits: readiness is deliberately conservative when a balance changes despite sufficient funds; incomplete history remains labelled; global Gifts limits may change after preview and are rechecked by the payment guard; report-required fulfillment remains manual; generic/imported DRAFT without creation provenance may require seller review of coarse/older order dates. No silent retries are introduced.

There is no atomic transaction spanning SQLite and Playerok. Local delivery is committed before publication; unknown publication preserves the prepared state and blocks another publication. Exact receipt is still required for every gift and PREMIUM expense. External manual publish/redraft cycles outside this application cannot be excluded solely by local creation history; do not claim a distributed zero-risk guarantee. New own first-publication tests prove controlled coarse-clock behavior, not arbitrary upstream chronology.

## Live boundaries
A fresh read-only Playerok query confirmed the configured account identity and exact username field; zero mutations. Historical 2026-10-30 free listing lifecycle is retained evidence only. Current paid PREMIUM/refunds/real Playerok customer messages remain untested and unauthorised. The sole permitted real payment is one Heart for @kzzkzok at exactly15 Stars after full checks; result is recorded separately and cannot be inferred from mock tests.

### Severity and acceptance scope
P0: cross-lot paid promotion authority (F8), aggregate spend/reservation bypass in launch, global STOP cancellation. P1: release revision loss/delete-before-read/newer-owner flag overwrite (F1–F3), non-atomic schedule enable (F4), valid optional username field rejected (F6), mutually enabled delivery modes, first-sale time precision, missing safe global resume, internal raw session-error UX and optional account lookup. All listed fixes have reproducing RED evidence and targeted GREEN results; the first complete regression run exposed additional navigation expectations and optional-account lookup, which were corrected openly.
Independent roles actually used: Financial Safety and Concurrency/Recovery were examined by one separate reviewer; User Journey QA and UX by two further reviewers. Final follow-up Financial reviewer reached a usage limit after handing off the timestamp counterexample; main engineer completed the narrow provenance fix with regression tests. That final code is not represented as a completed second independent approval.

## Подтверждённый live Heart — 2026-11-01

**LIVE SUCCESS только для реального Telegram-подарка.** Одна заранее разрешённая отправка Heart через production GiftService → OutboxWorker → PersonalGiftSender выполнена в изолированной SQLite. Все Playerok-ответы, публикация, PAID-заказ, чат и завершение были синтетическими; реальных мутаций Playerok0, сообщений покупателям0. Это НЕ доказательство настоящей продажи Playerok.

- Свежая авторизация совпала с ранее закреплённым личным аккаунтом; рабочая сессия была свободна и защищена lock. СОБСТВЕННЫЙ Playerok Proxy/SOCKS5 поднят и штатно закрыт.
- @kzzkzok разрешён в Telegram User ID **822906320**, другой получатель не подставлялся.
- Актуальный Heart ID **5170145012310081615**, цена формы и каталога **15 Stars**. Баланс до50, после35.
- Durable intent и однократный admission сохранены до платёжного RPC. Платёжных попыток **1**.
- Независимое чтение истории подтвердило MessageService/MessageActionStarGift, правильные User ID/gift ID/reference, message ID **16998**, дату **2026-11-01T01:35:11Z**, отсутствие refund. Простого PaymentResult для успеха было недостаточно.
- gift_job/gift_step **SUCCEEDED**, create/publish/gift_send/gift_notice/fulfill Outbox **SUCCEEDED** (create/publish/notice/fulfill — MOCK).
- Поддельный чат получил ровно одно «Все отправлено, спасибо за покупку!». После настоящего переоткрытия изолированной SQLite повторный worker ничего не выполнил.
- Свидетельство без секретов: LIVE_HEART_ACCEPTANCE_20261101.json. Приватный durable журнал data/final-heart-acceptance-20261101.sqlite3 остаётся вне Git. Существующий журнал запрещает новый запуск/покупку; скрипт не запускался повторно для реального платежа.

Настоящие PREMIUM-платежи, поднятия, возвраты и сообщения Playerok не выполнялись. Личный счёт Telegram изменился ровно на разрешённые15Stars; рабочие .env и Playerok SQLite не изменялись приложением приёмки.

## Итоговая матрица функциональности и остаточных рисков

| Функция | Реализована | Mock E2E | Реальный live | Остаточный риск |
|---|---|---|---|---|
| Мастер/фото/DRAFT/бесплатная публикация | Да | Dispatcher + Outbox + readback | Исторический полный цикл30.10 | Новый запуск проверен локально; реальной новой публикации не было |
| Настройка выдачи до публикации | Да | Атомарный commit, сбои, первый заказ, рестарт | Только чтение схемы Playerok | Нет общей транзакции Playerok/SQLite; сторонние изменения на сайте |
| Один Heart15Stars | Да | Полная покупка+receipt+повтор | **Подтверждено1.11**, receipt16998 | Это синтетический Playerok-заказ, а не настоящая продажа |
| Пакеты Gifts/UNKNOWN/частичная доставка | Да | Последовательность, лимиты, ACK loss, restart | Пакет на реальных деньгах не проверялся | Неопределённость требует ручной receipt-сверки, повтора нет |
| PREMIUM/автоподнятия | Да | Бюджет, scoped grant, изоляция, квитанции | Не выполнялись | Нет доказанного upstream maxAmount, live-платёж ещё не принят |
| Возвраты/завершение Playerok | Да, с ограничениями | Mock guards/receipt/readback | Не выполнялись | Отчёт/способ передачи могут требовать ручного действия |
| Склад/текст/старые заказы | Да | Сохранены регрессии Inventory/Enrollment/Outbox | Реальная выдача в этом проходе не выполнялась | Старые/импортированные DRAFT без provenance сохраняют строгий cutoff |
| Telegram UX/доступ/диагностика | Да | Dispatcher/FSM, старые callback,13рендеров | Реальный Telegram-клиент визуально не проходился | Offline-рендер не доказывает поведение клиента/живого callback |
| localVPN/личная сессия | Сохранены | Транспорт/отказы/lease/sessionlock | Реальное чтение и отправка черезSOCKS5 | Отказ сети оставляет неизвестный платёж заблокированным |

**Вердикт:** разрешённый изолированный Telegram live-тест успешно пройден. Для штатного контролируемого заказа приложение имеет проверенную цепочку и закрытые воспроизведённые P0/P1 этого прохода; полная эксплуатационная приёмка настоящей продажи Playerok не закрыта. Это не заявление абсолютной безопасности или100%соответствия всей исходной спецификации. Следующий реальный заказ/платёж/действие Playerok требует отдельного распоряжения владельца; данный проход завершается без нового цикла аудита.

Простая проверка интерфейса: запустить start.bat, открыть /start, перейти в Заказы→История, Сообщения→диалог, Ещё→Статистика→графики/лучшие товары, Подарки→Настроить. Не включать оплаты для визуальной проверки. Главная имеет5постоянных пунктов; служебные операции в Ещё→Инструменты. Не нажимать повторно live-orchestrator: однократное разрешение уже использовано.
