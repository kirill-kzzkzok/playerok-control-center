import json
import secrets

from app.database.store import Conflict
from app.services.edit_confirmation import is_ordinary_edit
from app.services.fulfillment import active_unknown_filter
from app.services.premium_review import PREFIX, reviewed, terminal_bump, uncertain_expenses
from app.services.reconciliation import Reconciliation, signature
from app.telegram.formatting import escaped
from app.telegram.keyboards import keyboard
from app.telegram.presentation import edit_readback_note, local_time, operation_name
from app.telegram.presentation import status as status_label


class OperationsPanel:
    def __init__(self, store, api):
        self.store, self.service = store, Reconciliation(store, api)

    async def callback(self, action, owner, state):
        self.service.owner(owner)
        parts = action.split(":")
        if len(parts) == 4 and parts[1] == "list":
            await state.clear()
            status, page = parts[2], int(parts[3])
            if status not in {"ALL", "UNCERTAIN"} or not 0 <= page <= 1_000_000:
                raise ValueError("Некорректный список операций")
            where, params = ("1=1", ()) if status == "ALL" else active_unknown_filter(self.store)
            values = self.store.rows(
                "SELECT q.id,q.kind,q.status FROM outbox q LEFT JOIN orders o ON o.id=q.target WHERE "
                + where
                + " ORDER BY q.created_at DESC,q.id DESC LIMIT 13 OFFSET ?",
                (*params, page * 12),
            )
            rows = [
                [
                    (
                        f"{operation_name(value['kind'])} · {status_label(value['status'])}",
                        "ops:show:" + value["id"],
                    )
                ]
                for value in values[:12]
            ]
            navigation = []
            if page:
                navigation.append(("← Назад", f"ops:list:{status}:{page - 1}"))
            if len(values) > 12:
                navigation.append(("Далее →", f"ops:list:{status}:{page + 1}"))
            if navigation:
                rows.append(navigation)
            rows.append([("Неопределённые", "ops:list:UNCERTAIN:0"), ("Все операции", "ops:list:ALL:0")])
            return "🧾 <b>История операций</b>\n" + (
                "Выберите запись." if values else "Операций нет."
            ), keyboard(rows)
        if len(parts) != 3:
            raise ValueError("Кнопка операции устарела")
        command, ident = parts[1:]
        if command == "apply":
            data = (await state.get_data()).get("resolution", {})
            if ident != data.get("nonce") or not data.get("reason"):
                raise Conflict("Подтверждение решения устарело")
            closing_edit = data["decision"] == "STOP_UNCERTAIN" and is_ordinary_edit(
                self.service.action(owner, data["id"])
            )
            await self.service.resolve(
                owner, data["id"], data["signature"], data["decision"], data["reason"], data.get("message_id")
            )
            await state.clear()
            if closing_edit:
                row = self.service.action(owner, data["id"])
                return (
                    "🗃 Запись правки закрыта без повторной отправки.\nТовар этой кнопкой не остановлен: разрешения выдачи не менялись. Исходная правка не объявлена успешной.\nПроверьте текущее объявление и привязку. Если вы отдельно выключали выдачу, включите её в настройках товара после проверки.",
                    keyboard(
                        [[("🔍 Проверить товар и выдачу", "rd:check:" + row["target"])]],
                        back=("⬅️ К товару", "item:" + row["target"]),
                    ),
                )
            return "Решение сохранено. Новые запросы отправки не создавались.", keyboard(
                [[("Операция", "ops:show:" + data["id"])]]
            )
        if command == "retrychecks":
            data = (await state.get_data()).get("retry_checks", {})
            if ident != data.get("nonce"):
                raise Conflict("Подтверждение устарело")
            self.service.retry_checks(owner, data["id"], data["signature"])
            await state.clear()
            return (
                "Разрешена новая проверка условий. Резерв сохранён; повтор неизвестной отправки не выполняется.",
                keyboard([[("Операция", "ops:show:" + data["id"])]]),
            )
        if command == "preparechecks":
            row = self.service.action(owner, ident)
            if row["started_at"] is not None or row["status"] != "PERMANENT_FAILED":
                raise Conflict("Исход отправки не позволяет безопасный повтор")
            nonce = secrets.token_hex(8)
            await state.set_data({"retry_checks": {"id": ident, "nonce": nonce, "signature": signature(row)}})
            return (
                "Внешняя отправка не начиналась. Повторно проверить условия и продолжить только при действующих разрешениях?",
                keyboard([[("Проверить и продолжить", "ops:retrychecks:" + nonce)]]),
            )
        row = self.service.action(owner, ident)
        if command == "show":
            await state.clear()
            resolutions = self.store.rows(
                "SELECT actor,decision,reason,resolved_at FROM operation_resolutions WHERE operation_id=?",
                (ident,),
            )
            uploads = self.store.rows(
                "SELECT position,status FROM chat_uploads WHERE operation_id=? ORDER BY position", (ident,)
            )
            closed_edit = is_ordinary_edit(row) and row["error_code"] == "MANUALLY_STOPPED_UNCERTAIN"
            shown_status = "Запись правки закрыта владельцем" if closed_edit else status_label(row["status"])
            shown_error = "Исходная правка не подтверждена" if closed_edit else row["error_code"] or "—"
            text = f"🧾 <b>Операция</b>\nID: <code>{escaped(ident, 100)}</code>\nТип: {escaped(operation_name(row['kind']), 60)}\nСтатус: {escaped(shown_status, 100)}\nЦель: {escaped(row['target'], 100)}\nСоздана: {local_time(row['created_at'])}\nПопыток: {row['attempts']}\nРезультат: {escaped(shown_error, 100)}"
            if (
                row["started_at"] is None
                and row["status"] == "PERMANENT_FAILED"
                and row["kind"] in {"delivery", "delivery_text", "chain_message", "reply", "mark_read"}
            ):
                text += "\nВнешняя отправка не начиналась. Резерв удерживается; доступна повторная проверка условий."
            if uploads:
                text += "\nВложения: " + ", ".join(
                    f"{upload['position'] + 1}: {upload['status']}" for upload in uploads
                )
            late = self.store.rows(
                "SELECT observed_status,result,observed_at FROM outbox_late_results WHERE operation_id=? ORDER BY observed_at DESC LIMIT 3",
                (ident,),
            )
            for observation in late:
                evidence = json.loads(observation["result"] or "{}")
                text += "\nПоздний ответ исполнителя: " + escaped(observation["observed_status"], 40)
                if evidence.get("message_id"):
                    text += " · ID сообщения: <code>" + escaped(evidence["message_id"], 100) + "</code>"
                text += "\nОсновной статус не изменён. Проверьте результат перед ручным решением."
            if row["error_code"] == "MANUALLY_STOPPED_UNCERTAIN":
                text += (
                    "\nЗакрыта только запись правки. Кнопка не выключает товар и не меняет разрешения выдачи."
                    if closed_edit
                    else "\nПовтор остановлен владельцем; внешний результат остался неизвестен."
                )
            if row["kind"] == "edit" and row["status"] != "SUCCEEDED":
                text += edit_readback_note(self.store.setting("edit:confirmation:" + ident, {}))
            if resolutions:
                resolution = resolutions[0]
                decision = "Закрыта запись правки" if closed_edit else resolution["decision"]
                text += f"\n\nРешение владельца {resolution['actor']}: {decision}\n{escaped(resolution['reason'], 600)}\n{local_time(resolution['resolved_at'])}"
            rows = []
            if (
                row["started_at"] is None
                and row["status"] == "PERMANENT_FAILED"
                and row["kind"] in {"delivery", "delivery_text", "chain_message", "reply", "mark_read"}
            ):
                rows.append([("Повторить безопасную проверку", "ops:preparechecks:" + ident)])
            if row["status"] == "UNCERTAIN":
                text += "\n\nПовтор запрещён, пока результат не установлен. Отсутствие записи на проверенных страницах не доказывает, что отправки не было."
                if row["kind"] in {
                    "create",
                    "edit",
                    "reply",
                    "delivery",
                    "delivery_text",
                    "chain_message",
                    "mark_read",
                    "edit_fields",
                    "fulfill",
                    "premium_publish",
                    "premium_bump",
                }:
                    rows.append([("🔍 Проверить результат на Playerok", "ops:confirm:" + ident)])
                if row["kind"] != "premium_bump":
                    rows.append(
                        [
                            (
                                "🗃 Закрыть запись правки"
                                if is_ordinary_edit(row)
                                else "⛔ Закрыть без повторной отправки",
                                "ops:stop:" + ident,
                            )
                        ]
                    )
                if row["kind"] == "premium_bump":
                    expense = next((r for r in uncertain_expenses(self.store) if r["id"] == ident), None)
                    if expense and reviewed(self.store, expense):
                        decision = self.store.setting(PREFIX + ident)
                        text += f"\n\nВладелец учёл {decision['amount']} ₽ в расходах. Исход прежней покупки остаётся неизвестным."
                    elif expense and terminal_bump(expense) and not expense["late"]:
                        rows.append([("Рассмотреть расход вручную", "payp:review:" + ident)])
            elif (
                row["error_code"] == "MANUALLY_STOPPED_UNCERTAIN"
                and row["kind"] in {"edit", "premium_publish", "premium_bump"}
                and row["status"] == "PERMANENT_FAILED"
            ):
                text += "\n\nЗапись закрыта без подтверждения успеха. Сверка без записи по-прежнему доступна; повторной отправки не будет."
                rows.append([("🔍 Проверить результат на Playerok", "ops:confirm:" + ident)])
                if closed_edit:
                    rows.append([("🔍 Проверить товар и выдачу", "rd:check:" + row["target"])])
            rows.append([("История операций", "ops:list:ALL:0")])
            return text, keyboard(rows)
        if command not in {"stop", "confirm"} or (
            row["status"] != "UNCERTAIN"
            and not (
                command == "confirm"
                and row["status"] == "PERMANENT_FAILED"
                and row["error_code"] == "MANUALLY_STOPPED_UNCERTAIN"
                and row["kind"] in {"edit", "premium_publish", "premium_bump"}
            )
        ):
            raise Conflict("Операция уже разрешена или действие недоступно")
        data = {
            "id": ident,
            "signature": signature(row),
            "decision": "STOP_UNCERTAIN",
            "ordinary_edit": is_ordinary_edit(row),
        }
        if command == "confirm":
            if row["kind"] == "edit":
                evidence = await self.service.evidence(owner, ident)
                if evidence["decision"] == "CURRENT_STATE_ONLY":
                    await state.clear()
                    return "🔍 Playerok проверен\n" + evidence["reason"], keyboard(
                        [
                            *(
                                [
                                    [
                                        (
                                            "🗃 Закрыть запись правки"
                                            if is_ordinary_edit(row)
                                            else "⛔ Закрыть без повторной отправки",
                                            "ops:stop:" + ident,
                                        )
                                    ]
                                ]
                                if row["status"] == "UNCERTAIN"
                                else []
                            ),
                            [("⬅️ К операции", "ops:show:" + ident)],
                        ]
                    )
            if row["kind"] in {
                "create",
                "edit",
                "edit_fields",
                "mark_read",
                "fulfill",
                "premium_publish",
                "premium_bump",
            }:
                data["evidence"] = await self.service.evidence(owner, ident)
                data["decision"] = "CONFIRM_READ_STATE"
            elif row["kind"] in {"reply", "delivery", "delivery_text", "chain_message"}:
                await state.set_state("resolve:message")
                await state.set_data({"resolution": data})
                return (
                    "Укажите ID отправленного сообщения в Playerok, которое лично проверили. Текст и данные выдачи присылать не нужно. Поиск ограничен 480 сообщениями; отсутствие совпадения не разрешает повтор.",
                    keyboard([]),
                )
            else:
                raise Conflict("Проверка успешности этого типа операции пока недоступна")
        await state.set_state("resolve:reason")
        await state.set_data({"resolution": data})
        if command == "stop" and data["ordinary_edit"]:
            return (
                "🗃 Закрыть запись правки?\n\nЭто не остановка товара. Разрешения выдачи этой кнопкой не меняются. Закрытие не отменяет изменения на Playerok и не подтверждает успех. Повторная правка не отправляется.\nРезервы платежей и карантин выдачи сохраняются. После закрытия проверьте текущую привязку товара.\n\n"
                + self.reason_prompt(data),
                keyboard([[("⬅️ Отмена", "ops:show:" + ident)]]),
            )
        if command == "stop":
            return (
                "⛔ Закрыть без повторной отправки?\n\nДействие на Playerok могло успешно выполниться. Закрытие не отменяет изменения и не подтверждает успех. Повторный запрос не отправляется.\n\nРезервы начатых и неизвестных платежей и карантин выдачи сохраняются. Только доказанно неиспользованный денежный резерв может быть снят существующей проверкой. Привязки не подтверждаются, автовыдача не включается. Для обычного редактирования закрытие снимает ожидание этой записи в очереди; перед новой правкой проверьте текущее объявление.\n\n"
                + self.reason_prompt(data),
                keyboard([[("⬅️ Отмена", "ops:show:" + ident)]]),
            )
        return self.reason_prompt(data), keyboard([])

    @staticmethod
    def reason_prompt(data):
        if data["decision"] == "STOP_UNCERTAIN":
            if data.get("ordinary_edit"):
                return "Опишите причину закрытия записи правки (10–500 символов). Товар не выключается; исходный результат не объявляется успешным. Не пишите секреты."
            return "Опишите причину остановки (10–500 символов). Внешний результат останется неизвестен; резерв останется в карантине. Повторная отправка не создаётся. Не пишите секреты или данные выдачи."
        return "API подтвердил доступное сообщение/состояние. Решение остаётся ручным. Если есть фото, содержимое файлов нужно лично сверить на Playerok: совпадение их числа этого не доказывает. Опишите проверку (10–500 символов), без секретов и данных выдачи."

    async def message(self, message, state):
        owner = message.from_user.id
        self.service.owner(owner)
        data = (await state.get_data()).get("resolution", {})
        if await state.get_state() == "resolve:message":
            evidence = await self.service.evidence(owner, data["id"], message.text)
            data.update(message_id=evidence["message_id"], decision=evidence["decision"], evidence=evidence)
            await state.update_data(resolution=data)
            await state.set_state("resolve:reason")
            return self.reason_prompt(data), keyboard([])
        reason = message.text or ""
        if not 10 <= len(reason.strip()) <= 500:
            raise ValueError("Комментарий должен содержать от 10 до 500 символов")
        data.update(reason=reason.strip(), nonce=secrets.token_hex(8))
        await state.update_data(resolution=data)
        await state.set_state(None)
        text = (
            "Подтвердите локальное решение: "
            + (
                "закрыть без подтверждения успеха"
                if data["decision"] == "STOP_UNCERTAIN"
                else "подтвердить проверенный результат"
            )
            + "\n"
            + escaped(reason, 700)
            + "\n\nВ Playerok ничего не отправляется. Перед подтверждением успешности данные будут проверены повторно."
        )
        return text, keyboard(
            [
                [
                    (
                        "🗃 Закрыть запись правки"
                        if data["decision"] == "STOP_UNCERTAIN" and data.get("ordinary_edit")
                        else "⛔ Закрыть запись без повтора"
                        if data["decision"] == "STOP_UNCERTAIN"
                        else "✅ Сохранить решение",
                        "ops:apply:" + data["nonce"],
                    )
                ]
            ]
        )
