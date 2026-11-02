# Telegram stability — 2026-11-02

База: 2ec0c1d. Полное ТЗ: TELEGRAM_STABILITY_SPEC.md. Проверки offline, внешние API подменяются только на тестовой границе. Рабочие .env/SQLite/session не читались и не менялись, live-операций нет.

## Подтверждённые дефекты и исправления

| Дефект | RED | Исправление |
|---|---|---|
| Permanent HTTP400 обновлений повторялся после backoff | test_telegram_stability,4варианта | Notifications UI refresh terminal отдельно от SUCCEEDED/истории |
| Явно отклонённый HTML оставлял editing=True | Dispatcher test_definite_bad_content | Navigation сбрасывает только rejected proposal; timeout сохраняет неизвестность |
| Исходные Telegram ошибки могли раскрыть текст через общий logger | test_raw_telegram_exception RED1 | SecretFilter классифицирует Telegram exception/args до console/file |
| Поиск читает20страниц за одно нажатие | test_catalog_search RED | <=3страниц и5сек между запросами, прежняя кнопка продолжения/partial note |
| UI protection сканирует историю | EXPLAIN SCAN RED | migration028:2coveringindexes(owner_id,message_id) |
| Backup блокирует UI через shared RLock | controlled-copy RED0.319s | отдельный read-only WAL source, источник существует, committed snapshot |
| Служебный launch draft виден обычному Wizard | test_listing_launch RED | скрыт из обычных drafts, старый callback перенаправлен в LaunchPanel, CANCELLED config не возвращается как рабочая |
| Ошибка записи FSM оставляет новый state со старыми данными | SQLite trigger RED | один SQL state+data в критических входах Wizard/Launch/Drafts |
| Часовые retry Enrollment неожиданно создают выдачу | test_old_pending_network_retry RED | >15мин от подтверждённой даты заказа: BLOCKED/OWNER_REVIEW_REQUIRED + событие, заказ сохранён |
| Read reconciliation соглашений обходит историю разрешения | AutoAgreements RED | CAS+operation_resolutions+audit в одной транзакции; previous error сохранён |
| uncertain APIError выбирает retry раньше uncertainty | OutboxWorker RED | uncertain приоритетен; started_at/Store.finish guards сохранены |

OperationResults имеет bounded exponential backoff/RetryAfter, максимум8попыток на конкретный view. Известный permanent edit прекращается; новое открытие с другим view восстанавливает UI. Результат Outbox и квитанция не удаляются. Stateful Dispatcher test проверяет удалённый view, восстановление /start и отсутствие повторного buyer send.

## Что уже исправлено и не переписывалось

- G1–G6/F1–F18 старого GLM отражены в FIX_TRACEABILITY.md и существующих исполняемых test_fix_*; текущая полная регрессия включает их.
- refresh_messages уже принимал MESSAGE_NOT_MODIFIED; это не новый дефект. Теперь классификатор общий.
- Payload правки/удаления сообщений уже обновляется и архивируется, defer_update уже изолирован по operation_id.
- Page iterator уже ограничен100страницами/60сек/10000записей; edit_fields уже требует readback; enum identity уже локализована.
- Store.finish/recover уже не допускает retry после started_at; сохранены поздние результаты и карантин неизвестной выдачи.
- ACK уже выполняется до FSM isolation, безопасная навигация не пропадает при ACK failure. Таймаут2с не увеличен. Cancellation ACK не называется падением прокси.
- Не сбрасываем rendered_status механически: новый token результата должен подтвердить актуальное представление, иначе гонка с worker может скрыть новое состояние. Идентичный основной render уже пропускается; результат после перехода может потребовать одного известного edit.
- UploadCleanup уже выполняется в thread. Сериализация Playerok намеренно держится до реального завершения sync call, даже при coroutine cancellation. Независимый авторизованный HTTP-клиент не создавался.
- Нельзя повторять неизвестное уведомление или выводить отсутствие мутации из одного4xx. Эти рекомендации GLM отклонены; неизвестный платёж/выдача остаётся для положительной сверки.

## Логи и измерения

Обычная консоль: бизнес-события сохранены; успешные HTTP200 не печатаются; классифицированная первая ошибка, раз в30сек сводка повторов(method+code), после успешного запроса того же метода — восстановление с числом отказов. Разные причины не объединяются. MESSAGE_NOT_MODIFIED и отменённый ACK — DEBUG. Известный отказ API не называется падением SOCKS. Proxy unavailable распознаётся по реальному guard/proxy exception; DNS/timeout — по типу trace exception.

DEBUG включается PCC_LOG_LEVEL=DEBUG в окружении запуска либо существующим `python -m app --telegram-network-diagnostics`. Не требуется менять .env. Подробности только в logs/control_center.log, ротация2MB/5копий; консоль INFO. Debug содержит безопасный request_id, method, route/host, duration/status/connection/cause; URL/body/token отсутствуют. UI timings: ACK, FSM wait, navigation wait, SQLite reads, Playerok wait/request, Telegram и total. Медленные>=3сек выводятся кратко с основным измеренным источником; остаток — подготовка UI. Это не сетевой SLA.

Сбой создания файла не останавливает приложение; runtime handler error выдаёт одно безопасное сообщение в stderr без payload. Общий аудит финансов остаётся в SQLite, формат логов его не заменяет.

Сравнение на временной SQLite:30000notifications/200одинаковыхlookup — SCAN0.436603s → coveringindex0.001906s. Контролируемый backup0.3s: прежняя UI-задержка0.319s → ниже0.15s в том же тесте. Поиск20 → максимум3страницы за нажатие. Это локальные измерения, не live Telegram/Playerok.

## Проверки и ограничения

- Фазовые наборы:35,19,41,92,103,31,44,22 passed; фактические RED и промежуточные ошибки сохранены в data/ и PROGRESS.md.
- Новая временная БД и upgrade старой копии — test_fix_migrations; WAL/integrity/FK/backup — selfcheck.
- Полный pytest/финальный CI записываются после завершения; успех заранее не заявляется.
- Не подтверждены live latency, поведение конкретного рабочего узла localVPN и настоящие серверные ответы Telegram/Playerok. Новых маршрутов/fallback, зависимостей или платных разрешений нет.

Проверка владельцем: перезапустить start.bat; /status → товар → Проверить готовность → назад; открыть результат операции и /start. Диагностика не отправляет товар и не меняет лоты. Для нового просмотра результата после удалённой панели используйте /start и существующую историю операций. Старые отложенные заказы требуют отдельного решения владельца и не стартуют сами после15мин.
