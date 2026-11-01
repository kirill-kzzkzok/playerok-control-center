# TRACEABILITY — CODEX_FIX_SPEC

База RED: dcb728305e0448066e73815b9fab15bd9b4ad8fc. Только offline tests с реальными Store/Dispatcher/FSM/Executor/Workers и подменой внешних транспортов. FIXED означает исправление и локальное подтверждение; не live-приёмку. Полные результаты и неуспешные промежуточные прогоны — FIX_PROGRESS.md.

Обозначения suites: N=test_fix_navigation; A=test_fix_adversarial; O=test_fix_observability; M=test_fix_money_repro; D=test_fix_dispatch_adversarial; C=test_fix_acceptance; P=test_fix_api; R=test_fix_migrations. Все находятся в tests/.

| ID | Итог | Причина / файлы | Исполняемое доказательство |
|---|---|---|---|
| G1 | FIXED | callback_ack: явные read/local capabilities; router: отказ сохраняет FSM/панель | N.ack_rejection_preserves_confirmation_and_panel; N.wizard_local_back; A.real_list_pagination; исходно 3 RED, затем GREEN |
| G2 | FIXED | navigation.allowed и Replies.origin проверяют точный owner/notification/message/chat/SUCCEEDED/not-deleted | N.notification_buttons; A.deleted_notification_cannot_commit; прежние test_ux_redesign/replies |
| G3 | FIXED | navigation: UNKNOWN не сбрасывает sending, предлагает явный recovery; нет повторного исходного send | test_telegram_transport.ui_response_timeout; test_global_ux_finance recovery; N |
| G4 | FIXED | navigation: определённый edit reject разрешает новую панель; bounded recovery text/caption fallback | N.start_replaces_deleted/uneditable; test_ux_followup; test_global_ux_finance |
| G5 | FIXED | ACK до FSM, dispatch-age начинается после FSM через CallbackAdmission; nonce/business guards остаются | C.fresh_ack_waiting_for_fsm: отдельный RED1 → GREEN; C.stale_ack; test_telegram_transport.ack_before_fsm_lock |
| G6 | FIXED | один ответ callback в ACK middleware; обратная связь отдельна от callback и основной панели | StatefulTelegram отклоняет второй ACK; N/C реальные Dispatcher + assertions по acks |
| F1 | FIXED | chat_events/message_notice/Notifications: известные server events локализованы при user=None; raw history сохранена | O.system_event (3), O.critical_system_problem_initial_baseline (RED1 → GREEN), исходный audit RED→GREEN |
| F2 | FIXED | Store.enqueue создаёт owner receipts для reply/mark_read; result route доступен | test_audit_red_repro (4 RED → GREEN); O.commit_worker_updates_same_panel (2) |
| F3 | FIXED | operation_receipts exact view_message_id/view_token; Notifications обновляет только ещё актуальный экран; origin не угадывается по чату | O.commit_worker_updates_same_panel; test_operation_results_ui; повтор commit даёт один внеш. send |
| F4 | FIXED (safe alternative) | operation_results: UNKNOWN уведомления не пересылается; результат доступен из результатов, show объясняет неопределённость уведомления; известный актуальный экран обновляется | test_operation_results_ui.uncertain_operation_and_unknown_telegram_ack; O; тест проверяет 1 send и отсутствие replay |
| F5 | FIXED | mark_read имеет receipt/view, исходное уведомление обновляется при точном origin | O.commit_worker_updates_same_panel[mark_read]; D.late_older_reply; C.backoff |
| F6 | FIXED | chat_panel использует локализацию известных событий, человеческий literal не скрывается | O.system_event; test_audit_red_repro |
| F7 | FIXED | Orders.ingest_message обновляет inbox payload, архивирует старую версию; уведомление редактируется только по известному ID; уже прочитанное/удалённое не посылается заново | O.edited_deleted_message_updates_known_notification; A.deleted_notification_cannot_commit; test_notifications |
| F8 | FIXED | Notifications.defer_update адресует operation_id; рендер берётся из всей истории, backoff только dirty subset | C.notification_backoff_isolated... с настоящим worker и управляемыми часами; D.late_older_reply (RED1 → GREEN) |
| F9 | REJECTED WITH EVIDENCE | сверка текущего orders.status уже существовала; позднее PAID не показывается как актуальное | C.obsolete_order_event_is_skipped; test_durability.terminal_status_not_downgraded |
| F10 | FIXED | migration026 + SafeAccount start-hook + Store.finish/recover: bounded preflight retry, RESERVED; после POST UNKNOWN/QUARANTINED | M.preflight (4 codes); M.safe_preflight_retry; D.cancel; test_dispatch_recovery abrupt subprocess. RED на исходном HEAD подтверждён |
| F11 | FIXED | Orders.reserve проверяет локальную/серверную проблему, direction/automation и seller до изменения запаса; повторный guard остаётся перед POST | M.problem_annotation; test_telegram_integration.order_notes... явно снимает проблему через UI; test_delivery_chains |
| F12 | FIXED | reserve отказывается от non-stock policy до empty_stock | M.manual_stock_reserve_cannot_disable_fixed_text; test_universal_delivery_e2e fixed_text/manual/gifts |
| F13 | FIXED | delivery_chains: unknown greeting не доказывает отправку секрета; неотправленный секрет удерживается RESERVED, не отдаётся другому покупателю | test_delivery_chains STOP/failure/unknown greeting; test_stock_auto_cycle; M |
| F14 | REJECTED WITH EVIDENCE (исходная трактовка) | HTTP4xx/429 сами по себе НЕ доказывают no-effect. После durable started любое ошибочное retry/failed предложение становится UNKNOWN. Подтверждённый no-effect текущим API не заявляется | test_dispatch_recovery.actual_write_transport_failure (HTTP errors); M.started_post_can_never_become_retryable; нет автоматического release после неизвестного POST |
| F15 | FIXED | Playerok.pages cap100 страниц/10000 записей/60с и контроль пустого продолжения; неполный scan не выдаётся за complete | P.endless_fresh_cursor (RED → GREEN); test_sync_recovery malformed/repeated/PARTIAL |
| F16 | FIXED | item_receipts.field_receipt_matches + независимый get_item; draft UPDATED атомарно только при положительном readback; Reconciliation проверяет поля/владельца | P.field_edit_silent_server_ignored_values (RED → GREEN); test_item_fields; test_reconciliation |
| F17 | FIXED | parser сохраняет icon_type_code ровно из wire, числовые enum/API не меняются | P.agreement_icon_keeps_wire_identity[CONFIRMATION/RESTRICTION] RED2 → GREEN. UI не использовал aliased .name, ложная UI-причина не заявляется |
| F18 | FIXED (liveness) | потерявшее authority чтение остаётся fail closed; CONNECTION_NOT_VERIFIED до POST разрешает bounded preflight retry с сохранением резерва; новая запись только после новой identity-проверки | M.preflight[CONNECTION_NOT_VERIFIED] RED → GREEN; test_playerok_connection + test_thread_calls |
| T1 | FIXED | настоящий result:show после reply/mark_read проверяется через Dispatcher | O.commit_worker_updates_same_panel |
| T2 | FIXED | user=None + автор-человек с тем же literal отдельно | O.system_event; test_audit_red_repro |
| T3 | FIXED | строгий Telegram возвращает not-modified при повторном точном edit; это подтверждённое обновление | C.notification_backoff... lost ACK edit → bounded повтор того же ID → not-modified → success |
| T4 | FIXED | ACK failure/stale + commit не ставит действие в очередь и не стирает FSM | N.ack_rejection; C.stale_ack; test_telegram_transport.unacknowledged_mutating |
| T5 | FIXED (E2E gap) | прежние unit tests с synthetic finish не объявлены E2E; добавлены реальные worker/HTTP-boundary пути | O.commit_worker_updates_same_panel; C.backoff; test_universal_delivery_e2e |
| T6 | FIXED (E2E gap) | unit fixture receipts для cleanup сохранены; production writer проверяется отдельно, не ручным notice_status='SENT' | test_operation_results_ui.real_handlers_queue_then_notify; O; UNKNOWN notice case |
| T7 | FIXED | управляемое старение ACK, свежий click за FSM, stale ACK и nonce replay разделены | C.fresh_ack_waiting_for_fsm / stale_ack; test_telegram_transport.ack_before_fsm_lock |
| T8 | FIXED | backoff проверяется часами без ручного SQL retry_after=0 | C.notification_backoff...; test_replies.notification_edit_after_reply... |

## Матрица A–T

| Путь | Доказательство / граница |
|---|---|
| A–D | N/A, test_global_ux_finance, test_ux_followup, test_telegram_transport: один актуальный экран, history commit после ACK, медиа, deleted/uneditable, UNKNOWN recovery, рестарт |
| E–I | N/O/C/D, test_replies, test_telegram_transport: точный origin, подтверждение перед send/read, один POST, локализация/критические события, возраст/lock/ACK |
| J–N | M/D, test_durability, test_stock_auto_cycle, test_delivery_chains, test_universal_delivery_e2e: scope/unique, PRE/POST, problem, non-stock |
| O | test_gifts, test_gift_bundles, test_gift_safety*, test_gift_empty_caption*, test_universal_delivery_e2e: соответствующие существующие реальные сервисы, только внешние fake transports |
| P–Q | P, test_sync_recovery, test_item_fields, test_reconciliation: bounded/incomplete и точные поля |
| R | R + D legacy migration/reconciliation, selfcheck: схема24→26, временная backup-копия, WAL/integrity/FK, сохранение orders/gifts/stock/logical/settings/operations |
| S | test_thread_calls, test_listing_launch, test_launch_fault_matrix, test_universal_delivery_e2e, D/M: STOP/CAS/поздний dispatch/рестарт |
| T | autouse tests/conftest блокирует nonloopback socket и рабочий dotenv/shell credentials; P проверяет блокировку; все DB из tmp_path. Subprocess tests используют только синтетический явный код/временные пути |

Exactly-once здесь означает отсутствие автоматического повторного внешнего dispatch при доказанном started/UNKNOWN. Полной распределённой атомарности с Telegram/Playerok не обещаем: неизвестный результат может потребовать ручной сверки по положительному доказательству; отсутствие квитанции не разрешает повтор.

## Контроль задержек

Воспроизводимый tests/test_fix_latency.py: ACK задержан на40мс при таймауте20мс, API-чтение задержано на30мс. Фактический изолированный прогон: items91.808мс, item:lot93.099мс, compose:chat64.924мс, 3passed/exit0/7.44s с setup. Улучшение — безопасные чтения/FSM-подготовка завершаются несмотря на отказ ACK; baseline RED доказывает прежний drop пагинации/локального Back. Это не оценка настоящего SOCKS/Telegram, не SLA и не сравнение live-скорости. Full-suite timing нельзя интерпретировать как задержку интерфейса.

## Итоговый проверенный набор

Окончательный локальный полный pytest: **1521 passed, 0 failed, 0 skipped, exit0,306.51s** (`data/fix-final-pytest.txt`). Независимая адресная проверка96passed/exit0/38.51s. Ruffcheck/format265files, compileall, pipcheck и offline selfcheckschema1..26/backup/integrity — exit0. Промежуточные RED и неуспешные полные прогоны не скрыты: FIX_PROGRESS.md. CI нового коммита проверяется отдельно после push. Нет live-подтверждения API-мутаций/доставки; реальные ограничения и ручная проверка описаны в MANUAL_SMOKE_TEST.md.
