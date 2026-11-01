import json
import secrets

from app.database.store import Conflict
from app.services.reconciliation import Reconciliation, signature
from app.telegram.formatting import escaped
from app.telegram.keyboards import keyboard
from app.telegram.presentation import operation_name
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
            where = "1=1" if status == "ALL" else "status='UNCERTAIN'"
            values = self.store.rows(
                "SELECT id,kind,status FROM outbox WHERE "
                + where
                + " ORDER BY created_at DESC,id DESC LIMIT 13 OFFSET ?",
                (page * 12,),
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
            await self.service.resolve(
                owner, data["id"], data["signature"], data["decision"], data["reason"], data.get("message_id")
            )
            await state.clear()
            return "Решение сохранено. Новые запросы отправки не создавались.", keyboard(
                [[("Операция", "ops:show:" + data["id"])]]
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
            text = f"🧾 <b>Операция</b>\nID: <code>{escaped(ident, 100)}</code>\nТип: {escaped(row['kind'], 60)}\nСтатус: {escaped(row['status'], 60)}\nЦель: {escaped(row['target'], 100)}\nСоздана: {escaped(row['created_at'], 60)}\nПопыток: {row['attempts']}\nОшибка: {escaped(row['error_code'] or '—', 100)}"
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
                text += "\nПовтор остановлен владельцем; внешний результат остался неизвестен."
            if resolutions:
                resolution = resolutions[0]
                text += f"\n\nРешение владельца {resolution['actor']}: {resolution['decision']}\n{escaped(resolution['reason'], 600)}\n{escaped(resolution['resolved_at'], 60)}"
            rows = []
            if row["status"] == "UNCERTAIN":
                text += "\n\nПовтор запрещён, пока результат не установлен. Отсутствие записи на проверенных страницах не доказывает, что отправки не было."
                if row["kind"] in {
                    "reply",
                    "delivery",
                    "delivery_text",
                    "chain_message",
                    "mark_read",
                    "fulfill",
                    "premium_publish",
                    "premium_bump",
                }:
                    rows.append([("🔎 Сверить успешный результат", "ops:confirm:" + ident)])
                rows.append([("⛔ Остановить без повтора", "ops:stop:" + ident)])
            elif (
                row["error_code"] == "MANUALLY_STOPPED_UNCERTAIN"
                and row["kind"] in {"premium_publish", "premium_bump"}
                and row["status"] == "PERMANENT_FAILED"
            ):
                text += "\n\nПовтор остановлен, но платёж остался неизвестным. Сверка квитанции по-прежнему доступна."
                rows.append([("🔎 Сверить квитанцию и закрыть расход", "ops:confirm:" + ident)])
            rows.append([("История операций", "ops:list:ALL:0")])
            return text, keyboard(rows)
        if command not in {"stop", "confirm"} or (
            row["status"] != "UNCERTAIN"
            and not (
                command == "confirm"
                and row["status"] == "PERMANENT_FAILED"
                and row["error_code"] == "MANUALLY_STOPPED_UNCERTAIN"
                and row["kind"] in {"premium_publish", "premium_bump"}
            )
        ):
            raise Conflict("Операция уже разрешена или действие недоступно")
        data = {"id": ident, "signature": signature(row), "decision": "STOP_UNCERTAIN"}
        if command == "confirm":
            if row["kind"] in {"mark_read", "fulfill", "premium_publish", "premium_bump"}:
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
        return self.reason_prompt(data), keyboard([])

    @staticmethod
    def reason_prompt(data):
        if data["decision"] == "STOP_UNCERTAIN":
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
            + escaped(data["decision"], 60)
            + "\n"
            + escaped(reason, 700)
            + "\n\nВ Playerok ничего не отправляется. Перед подтверждением успешности данные будут проверены повторно."
        )
        return text, keyboard([[("✅ Сохранить решение", "ops:apply:" + data["nonce"])]])
