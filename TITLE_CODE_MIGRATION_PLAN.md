# Постоянный код в названии — карта перехода

База f622e03. Полное ТЗ: TITLE_CODE_MIGRATION_SPEC.md.

## Единственный активный источник
После переключения marker принимает только название и возвращает нормализованные три символа. Все новые matching/verify/create/edit вызывают один parser. Description остаётся контрольным содержимым, но не источником идентификатора. Исторические order scopes/intent/receipt не переписываются. gift_code по-прежнему обозначает тип подарка.

## Этап B — завершённый примитив, ещё не переключающий runtime
product_codes.py: строгий suffix, UTF16 лимит с сохранением Unicode, exact removal архивной последней строки, cryptographic choice, SQLite reservations с bounded collisions. Migration029 только добавляет резерв кодов: не меняет товары, выдачу, разрешения, имена или описания. Таблица не допускает DELETE/смены identity и не имеет cascading FK: удалённый UUID не освобождает код. PREPARE может выбрать свободный кандидат, COMMIT резервирует его атомарно; коллизия подтверждённого превью требует нового подтверждения. Для создания нового серверного DRAFT код можно выделить внутри локального COMMIT и показать перед публикацией.

## Этап C — требуемая схема и зависимости
Старый logical_products.code имеет CHECK #7 и не может использоваться для новых кодов. Нужна rebuild этого parent table с прежними UUID и FK, архивирование старого code в отдельном durable migration journal. Нельзя просто ALTER/массово заменить строки. Foreign keys отключаются только вне migration transaction, после атомарной rebuild обязательны FK/integrity проверки и восстановление режима; при ошибке rollback сохраняет старую схему. Legacy rows до подтверждения сервером имеют CODE_MIGRATION_REQUIRED, не разрешают новые выдачи. Новый code NULL допустим только до выделения нового постоянного кода; сравнение None==None не должно подтверждать identity.

Обязательные вызовы: LogicalProducts prepare_creation/commit_creation/creation_proof/prepare/commit/binding_guard/confirm_binding/verify_listing/inherit/order_scope; worker logical_products (catalog и paid deal); Outbox edit description protection; router edit name/description; LogicalProductsPanel preview; ListingsPanel search; ProductReadiness; Gifts configured_gifts/description snapshots; PlatformOrders. Полный список: локальный data/title-code-all-callers.txt.

## Этап D — сделки
Маркер из server deal.item.name используется как candidate hint. deliverable/platform_sale, owner/account, exact deal/itemID, critical category/obtaining/fields/price/name, future cutoff, logical config and spend guards сохраняются. Недостаточно данных → get_delivery_deal. get_item(privateID)=404 не разрешает подмену публичной карточкой. Старая description marker не fallback новой покупки.

## Этап E — durable plan
Связанные лоты планируются по product UUID, внешний edit name+description идёт через существующий Outbox с snapshot preflight и intent.started_at. Общий preview/одно подтверждение, блок на новые исполнения до полного readback всех связанных editable лотов. Активные/неизвестные операции и недоставленные заказы запрещают миграцию конкретного product; исторические succeeded receipts не меняются. Noneditable SOLD не объявляется перенесённым, отображается ручной pending-next-placement. UNKNOWN никогда не повторяется; положительный свежий readback завершает тот же edit. Автоматизация сохраняет прежний enabled/inherit/armed_at и прочие поля; выключенное не включается.

## Что ещё не реализовано на checkpoint B
Схема parent rebuild, переключение всех production callers, миграционный journal/guards/Outbox reconciliation, мастер/редактирование, массовый Telegram preview/confirmation и полная приёмка. Наличие parser/резерва не означает готовность перехода или перенесённый магазин. Живые лоты не изменялись.

## Актуальное дополнение владельца (02.11.2026)
Общий Telegram preview/confirmation отменён. Разрешена автоматическая одноразовая миграция существующих объявлений через существующий updateItem Outbox без дополнительных подтверждений. app.migrate_codes выполняется из каталога магазина с его прежней конфигурацией, эксклюзивным InstanceLock и предварительным backup; отдельные чужие pending operations не исполняются. Новые расходы/публикации/сообщения не разрешены.
Итоговая реализация:029 immutable reservation;030 atomic parent rebuild+durable journals;031 exact per-listing title snapshot. Runtime использует только title suffix. CodeMigration.run обрабатывает независимые продукты, unknown сверяет read-only, при отсутствии точного readback оставляет UNCERTAIN. При полном изменении — VERIFIED; неизменяемый архив остаётся MANUAL_REVIEW_REQUIRED. Старые historical order scopes/receipts не переписываются. Запуск повторяем; уже подтверждённые объявления не изменяются второй раз. Утверждения раннего checkpoint B выше — исторический прогресс, а не актуальный список неготовых runtime функций.
Live требования: никакого второго процесса на рабочей SQLite. Принудительное прерывание активного платёжного RPC ради миграции не выполняется. При конфликтах/незавершённой выдаче товар остаётся на сверке, остальные независимые переносятся. Цена/подарок/политики/разрешения не изменяются.
