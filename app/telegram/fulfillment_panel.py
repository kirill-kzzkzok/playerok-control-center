import secrets
from html import escape

from app.playerok.adapter import APIError
from app.services.fulfillment import SELLER_CHECK, Fulfillment, report_field
from app.services.products import ValidationError
from app.telegram.keyboards import keyboard

PROMPTS = {
    "workerName": "Введите имя исполнителя (2–40 символов).",
    "telegram": "Введите Telegram username исполнителя (до 64 символов).",
    "supplierOrderNumber": "Введите номер заказа поставщика (до 64 символов).",
    "price": "Введите фактическую стоимость у поставщика (0,01–100000, до двух знаков). Это данные отчёта; оплата не выполняется.",
}


class FulfillmentPanel:
    def __init__(self, store, api):
        self.service = Fulfillment(store, api)

    async def show(self, form, state):
        nonce = secrets.token_hex(6)
        form["nonce"] = nonce
        await state.set_data({"fulfillment": form})
        step = form["step"]
        await state.set_state("fulfill:" + step if step in PROMPTS else None)
        if step == "check":
            return (
                "✅ <b>Отметка выполнения продавцом</b>\nТолько после фактической передачи товара. Окончательное подтверждение остаётся за покупателем.",
                keyboard(
                    [
                        [(("☑ " if form["attested"] else "☐ ") + SELLER_CHECK, f"ff:{nonce}:check")],
                        [("Продолжить", f"ff:{nonce}:continue")],
                    ]
                ),
            )
        if step in PROMPTS:
            return PROMPTS[step], keyboard([])
        if step == "provider":
            if "providers" not in form:
                try:
                    form["providers"] = await self.service.providers()
                except (APIError, ValidationError):
                    return (
                        "Поставщики недоступны. Произвольные значения не подставляются; введённые поля сохранены.",
                        keyboard([[("🔄 Повторить чтение", f"ff:{nonce}:refresh")]]),
                    )
                await state.update_data(fulfillment=form)
            page, items = form.get("page", 0), form["providers"]
            rows = [
                [(item["name"][:60], f"ff:{nonce}:provider:{index}")]
                for index, item in enumerate(items)
                if page * 8 <= index < (page + 1) * 8
            ]
            navigation = []
            if page:
                navigation.append(("← Назад", f"ff:{nonce}:page:{page - 1}"))
            if (page + 1) * 8 < len(items):
                navigation.append(("Далее →", f"ff:{nonce}:page:{page + 1}"))
            return "Выберите поставщика из актуального списка.", keyboard(
                rows + ([navigation] if navigation else [])
            )
        if step == "currency":
            return "Выберите валюту стоимости поставщика.", keyboard(
                [[(value, f"ff:{nonce}:currency:{value}") for value in ("USD", "EUR", "KZT")]]
            )
        values = form["values"]
        mode = {
            "legacy": "Отметка SENT без отчёта",
            "draft": "Черновой отчёт исполнения",
            "fields": "Отчёт с данными исполнения",
        }[form["mode"]]
        text = "<b>Подтвердите отметку выполнения</b>\n" + escape(SELLER_CHECK) + "\nСпособ: " + mode
        if values:
            labels = {
                "workerName": "Исполнитель",
                "telegram": "Telegram",
                "supplierOrderNumber": "Заказ поставщика",
                "price": "Стоимость",
                "currency": "Валюта",
            }
            text += "\n" + "\n".join(
                escape(label) + ": " + escape(values[name]) for name, label in labels.items()
            )
            text += "\nПоставщик: " + escape(form["provider_name"])
        text += "\n\nПродавец отмечает исполнение SENT. Покупатель подтверждает получение. Перед записью сделка и требования будут проверены заново. Денежные операции не выполняются."
        return text, keyboard([[("✅ Отметить выполнение", f"ff:{nonce}:submit")]])

    async def callback(self, action, owner, state):
        self.service.orders.owner(owner)
        parts = action.split(":")
        if len(parts) == 3 and parts[1] == "start":
            prepared = await self.service.prepare(owner, parts[2])
            return await self.show(
                {
                    "id": prepared["id"],
                    "snapshot": prepared["snapshot"],
                    "mode": prepared["mode"],
                    "step": "check",
                    "attested": False,
                    "values": {},
                },
                state,
            )
        form = (await state.get_data()).get("fulfillment", {})
        if len(parts) not in {3, 4} or parts[1] != form.get("nonce"):
            raise ValidationError("Кнопка выполнения устарела")
        command = parts[2]
        if command == "check" and form["step"] == "check":
            form["attested"] = not form["attested"]
        elif command == "continue" and form["step"] == "check":
            if form["attested"] is not True:
                raise ValidationError("Нужно лично подтвердить передачу товара")
            form["step"] = "workerName" if form["mode"] == "fields" else "preview"
        elif command == "refresh" and form["step"] == "provider":
            form.pop("providers", None)
        elif len(parts) == 4 and command == "page" and form["step"] == "provider":
            page = int(parts[3])
            if not 0 <= page <= (len(form.get("providers", [])) - 1) // 8:
                raise ValidationError("Страница недоступна")
            form["page"] = page
        elif len(parts) == 4 and command == "provider" and form["step"] == "provider":
            index = int(parts[3])
            if not 0 <= index < len(form.get("providers", [])):
                raise ValidationError("Поставщик недоступен")
            provider = form["providers"][index]
            form["values"]["providerId"], form["provider_name"] = provider["id"], provider["name"]
            form["step"] = "supplierOrderNumber"
        elif len(parts) == 4 and command == "currency" and form["step"] == "currency":
            form["values"]["currency"] = report_field("currency", parts[3])
            form["step"] = "preview"
        elif command == "submit" and form["step"] == "preview":
            ident = await self.service.queue(
                owner, form["id"], form["snapshot"], form["values"], form["attested"], form["nonce"]
            )
            await state.clear()
            return (
                "Отметка поставлена в очередь. Результат проверяется по состоянию сделки и отчёту; неизвестный результат автоматически не повторяется.",
                keyboard([[("Операция", "ops:show:" + ident), ("Заказ", "order:" + form["id"])]]),
            )
        else:
            raise ValidationError("Действие не относится к текущему шагу")
        return await self.show(form, state)

    async def message(self, message, state):
        self.service.orders.owner(message.from_user.id)
        form = (await state.get_data())["fulfillment"]
        step = form["step"]
        if step not in PROMPTS:
            raise ValidationError("На этом шаге используйте кнопки")
        form["values"][step] = report_field(step, message.text or "")
        form["step"] = {
            "workerName": "telegram",
            "telegram": "provider",
            "supplierOrderNumber": "price",
            "price": "currency",
        }[step]
        return await self.show(form, state)
