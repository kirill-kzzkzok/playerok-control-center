# Финальная адресная стабилизация: проверяемая приёмка

Дата: 03.11.2026. База: ed1cb60cdcbb662751a394c900a3370c7368b9f4.
ТЗ: [FINAL_OPERATION_READINESS_SPEC.md](FINAL_OPERATION_READINESS_SPEC.md).

## Исправления

- Enrollment: возраст >900с не блокирует подтверждённую современную привязку с исходным периодом разрешения. Для неподтверждённых/старых политик ограничение сохраняется. Серверные проверки и атомарные guards исполняются перед выдачей.
- Противоречащие даты сделки сохраняют признак конфликта после повторной синхронизации и перезапуска. Приём по неопределённой дате запрещён; пропущенная дата может дополниться достоверной детализацией.
- LEGACY Gift: история и платежи сохраняются; диагностика направляет в существующий подтверждаемый переход на [XXX]. Самостоятельного переименования реальных объявлений нет.
- Новые критические события обслуживаются перед старыми редактированиями. Фоновые фазы карточек переписок, квитанций и старых экранов ограничены отдельными бюджетами 2с. Таймаут потенциальной отправки остаётся UNCERTAIN. Обычным событиям сохранена квота.
- PromotionWorker: DEGRADED, число ошибочных товаров, последнее успешное чтение, следующий повтор; одно предупреждение после3 ошибок, нормализация после восстановления. RetryAfter учитывается. Оплата остаётся в прежнем guarded Outbox.
- Ручной новый товар получает ту же постоянную идентичность [XXX], без разрешения выдачи. Название укладывается в72UTF16, в том числе emoji. Старые серверные объявления не меняются.

## Карта сквозной проверки

Все перечисленные тесты исполняют production services/workers/SQLite; Telegram-переходы проверяют Dispatcher/FSM. Имитации находятся на внешних транспортных границах. Таблица указывает исполняемые доказательства, а не live-приёмку. Итоговое прохождение определяется полным pytest и CI на финальном HEAD.

| Требование | Исполняемые проверки |
|---|---|
|1–5 Создание склада, текста, Gift, ручного и Playerok|test_final_all_creation_modes.py; test_final_platform_creation.py; test_operational_manual_identity.py|
|6 DEFAULT/PREMIUM через ListingLaunch|test_final_publish_authority.py; test_final_paid_draft_launch.py; test_listing_launch.py|
|7 Первая продажа каждого поддерживаемого типа|test_final_all_creation_modes.py; test_final_platform_creation.py; test_final_acceptance_e2e.py|
|8 Последняя единица на2заказа|test_universal_delivery_e2e.py; test_delivery_chains.py|
|9 Неизвестная отправка аккаунта|test_final_future_messages.py; test_universal_delivery_e2e.py|
|10 Неизвестная покупка Gift|test_final_acceptance_e2e.py; test_operational_downtime.py|
|11 Последовательные Stars-операции|test_final_outbox_resource_lanes.py; test_gift_bundles.py|
|12 Медленный Gift не задерживает склад|test_final_outbox_resource_lanes.py: RPC удерживается Event до независимой выдачи; отдельный вариант ждёт настоящий90-секундный дедлайн Executor и проверяет UNCERTAIN без повтора; это офлайн-замер, не live|
|13 50оплат|test_final_paid_load.py|
|14 Повтор Sync/WS|test_final_acceptance_e2e.py; test_final_paid_load.py; test_realtime.py|
|15–16 Перезапуск ожидающей/начатой операции|test_stability_enrollment_restart.py; test_final_outbox_resource_lanes.py; test_operational_downtime.py|
|17 Playerok сеть/восстановление|test_final_sync_isolation.py; test_enrollment.py; test_playerok_connection.py|
|18 Telegram сеть/восстановление|test_final_notice_outcomes.py; test_chat_cards.py; test_operational_notification_latency.py|
|19 Старый недоступный чат|test_final_sync_isolation.py; test_final_notification_baseline.py|
|20 Другой подтверждённый аккаунт|test_playerok_connection.py; test_account_binding.py|
|21 STOP|test_final_mass_stop_confirmation.py; test_operational_downtime.py; test_paid_promotions.py|
|22 Благодарность только будущим|test_final_future_messages.py|
|23 Перевыставление [XXX]|test_universal_delivery_e2e.py; test_playerok_delivery.py; test_final_product_overviews.py|
|24 Платное поднятие и лимиты|test_paid_promotions.py; test_operational_promotion_health.py|
|25 Старые миграции|test_fix_migrations.py; test_final_notice_outcomes.py; test_operational_legacy.py|
|26 Backup и восстановление|test_final_backup_retention.py; test_fix_migrations.py; app.selfcheck|
|27 Оплаченные проблемы без уведомления|test_final_paid_attention.py; test_operational_legacy.py|
|28 Нет двойной выдачи/платежа/поднятия|test_gift_bundles.py; test_paid_promotions.py; test_operational_downtime.py; test_final_outbox_resource_lanes.py|

## Границы доказательств

Не выполнялись настоящие платежи, выдачи, публикации, изменение описаний и сообщения покупателям. Рабочие SQLite/.env/сессии не открывались. Изолированный selfcheck проверяет миграции1–33, целостность WAL и повторное открытие backup;0внешних запросов.

Контролируемый первый запуск: резервная копия; один готовый Gift, один складской товар и один Playerok; сначала проверить диагностику/аккаунт/лимиты/STOP, затем отдельно разрешённые владельцем реальные заказы. Проверять квитанцию, правильного получателя, один расход/одну единицу, независимость уведомления от выполнения, сохранность после перезапуска. Старые LEGACY перевести через подтверждение кода до новых продаж. Не снимать UNKNOWN для тестирования.

Наблюдать: возраст оплаченного заказа без подтверждённой выдачи, раздел внимания, UNKNOWN/карантин, последнюю успешную синхронизацию, цикл workers, promotions:health, notifications:timing, расход Stars/лимиты/резервы и резервные копии. Метрики уведомлений отражают условия конкретного цикла, не гарантированное время доставки Telegram.

Офлайн-регрессия не даёт статуса READY_FOR_AUTONOMOUS_OPERATION. VPS допустим как следующий контролируемый запуск после зелёных финальных проверок, с единственным процессом для одной базы и без одновременного запуска локальной копии. Независимого ревью в этом запуске нет: выполнено собственное adversarial review, субагенты не запускались по требованию владельца.

Протоколы окончательного прогона: data/final-operational-acceptance.txt и .exit, data/final-operational-selfcheck.json; GitHub Actions — Offline acceptance на итоговом HEAD. Точные результаты и вердикт выдаются после завершения этих проверок; предыдущий baseline SUCCESS их не заменяет.
