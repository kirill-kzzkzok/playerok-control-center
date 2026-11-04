# Матрица приёмки новой переработки

База: ce4b749. PASS разрешён только с конкретным доказательством нового контракта; старый зелёный CI не подменяет новую приёмку.

| ID | Статус | Доказательство / следующий шаг |
|---|---|---|
| ID | NOT VERIFIED | Карта исходных сервисов/тестов: SELLER_AUTONOMY_AUDIT.md; требуется финальная проверка нового контракта |
| AUDIT | NOT VERIFIED | Карта исходных сервисов/тестов: SELLER_AUTONOMY_AUDIT.md; требуется финальная проверка нового контракта |
| HOME | NOT VERIFIED | 582d2d0: шесть целевых входов, Orders → Attention; краткие актуальные метрики ещё не завершены |
| ITEM | NOT VERIFIED | 582d2d0: четыре основных действия; настроенный склад/подарок, прямая выдача, terminal-сверка. Профиль93passed; финальная регрессия впереди |
| UI | FAIL | A03/A08: дублирование входов и разделов черновиков |
| STATE | NOT VERIFIED | d56040f: local STOP соблюдается Gift-worker, SPENDING отделён от UNKNOWN, остановленный оплаченный долг виден; профиль60passed. Остальные пути в финальной регрессии |
| CREATE | NOT VERIFIED | 16ce184: supported completion/inheritance defaults и подтверждённая выдача до SENT;127passed. Полный новый путь и упрощение Wizard ещё не приняты |
| GIFTS | NOT VERIFIED | Карта исходных сервисов/тестов: SELLER_AUTONOMY_AUDIT.md; требуется финальная проверка нового контракта |
| STOCK | NOT VERIFIED | Карта исходных сервисов/тестов: SELLER_AUTONOMY_AUDIT.md; требуется финальная проверка нового контракта |
| TEXT | NOT VERIFIED | Карта исходных сервисов/тестов: SELLER_AUTONOMY_AUDIT.md; требуется финальная проверка нового контракта |
| PLATFORM | NOT VERIFIED | Карта исходных сервисов/тестов: SELLER_AUTONOMY_AUDIT.md; требуется финальная проверка нового контракта |
| COMPLETE | NOT VERIFIED | 16ce184: Gift/Playerok и stock/text завершение только при подтверждении/без отчёта; old reminder не даёт новых прав. Профиль127passed, финальный CI впереди |
| SCOPE | NOT VERIFIED | Карта исходных сервисов/тестов: SELLER_AUTONOMY_AUDIT.md; требуется финальная проверка нового контракта |
| RENAME | PASS | 262df19: реальные Gift/stock/text workers, scanner, гонки и STOP;24focused+76adjacent, CI41840599484 SUCCESS |
| RECOVER | NOT VERIFIED | a440e8d: доказанно неначатый Gift preflight восстанавливается автоматически, неизвестный платёж не повторяется;152+31passed. Non-Gift retry caps/partial packages ещё открыты |
| CALLBACK | NOT VERIFIED | e9539a4:76passed; offline40actions ACKp95.001957s/local.013611s/card.026105s,1read/card; stalled A не задерживает B. Native/live измерения не выполнены |
| NAV | NOT VERIFIED | Карта исходных сервисов/тестов: SELLER_AUTONOMY_AUDIT.md; требуется финальная проверка нового контракта |
| VPN | NOT VERIFIED | Карта исходных сервисов/тестов: SELLER_AUTONOMY_AUDIT.md; требуется финальная проверка нового контракта |
| SETTINGS | NOT VERIFIED | Карта исходных сервисов/тестов: SELLER_AUTONOMY_AUDIT.md; требуется финальная проверка нового контракта |
| PROMOTE | NOT VERIFIED | Карта исходных сервисов/тестов: SELLER_AUTONOMY_AUDIT.md; требуется финальная проверка нового контракта |
| MESSAGES | NOT VERIFIED | Карта исходных сервисов/тестов: SELLER_AUTONOMY_AUDIT.md; требуется финальная проверка нового контракта |
| NOTICES | NOT VERIFIED | Карта исходных сервисов/тестов: SELLER_AUTONOMY_AUDIT.md; требуется финальная проверка нового контракта |
| FINANCE | NOT VERIFIED | Карта исходных сервисов/тестов: SELLER_AUTONOMY_AUDIT.md; требуется финальная проверка нового контракта |
| RELIST | NOT VERIFIED | Карта исходных сервисов/тестов: SELLER_AUTONOMY_AUDIT.md; требуется финальная проверка нового контракта |
| DATA | NOT VERIFIED | Карта исходных сервисов/тестов: SELLER_AUTONOMY_AUDIT.md; требуется финальная проверка нового контракта |
| SLA | NOT VERIFIED | Карта исходных сервисов/тестов: SELLER_AUTONOMY_AUDIT.md; требуется финальная проверка нового контракта |
| METRICS | FAIL | Сравнение всех сценариев до/после ещё не выполнено |
| ACCEPT | FAIL | Новая переработка ещё не прошла итоговую приёмку |
| CURRENT | NOT VERIFIED | Карта исходных сервисов/тестов: SELLER_AUTONOMY_AUDIT.md; требуется финальная проверка нового контракта |

## Исходные метрики

- Основное меню: пять постоянных кнопок плюс условная техническая кнопка проблем.
- Деклараций inline-кнопок:460 (статический AST, не число исполненных маршрутов).
- Основная карточка: readiness/выдача/прибыль/редактирование/продвижение/история/прочее и контекстные кнопки.
- Безопасное переименование: требуется отдельное подтверждение title_apply.
- Полная базовая регрессия:2133passed/0failed/0skipped; CI41827473939SUCCESS.
- p50/p95, полное число экранов/кликов/подтверждений,50 независимых настроенных лотов, native live и два рабочих лота: NOT VERIFIED.
- Внешняя среда не считается проверенной offline-транспортами.
