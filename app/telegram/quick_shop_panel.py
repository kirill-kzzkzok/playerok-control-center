"""Small owner tools; all remote writes stay in the existing wizard/Outbox."""

import secrets

from app.database.store import Conflict
from app.services.automation import Automation
from app.services.logical_products import LogicalProducts
from app.services.products import ValidationError, priority_price_basis
from app.telegram.formatting import escaped
from app.telegram.keyboards import keyboard


class QuickShopPanel:
    def __init__(self, store, api, wizard, gifts):
        self.store, self.api, self.wizard, self.gifts = store, api, wizard, gifts

    async def relist(self, action, owner, state):
        LogicalProducts(self.store, self.api).authority(owner)
        parts = action.split(":")
        if len(parts) == 3 and parts[1] == "item":
            item = await Automation(self.store, self.api).item(owner, parts[2])
            if item.get("status") != "SOLD" or item.get("may_be_published") is not True:
                raise ValidationError(
                    "Playerok не разрешил штатное повторное размещение. Проверьте товар на сайте"
                )
            quotes = await self.wizard.products.priority_preview(parts[2], priority_price_basis(item))
            nonce = secrets.token_hex(6)
            await state.set_state(None)
            await state.set_data(
                {
                    "relist": {
                        "id": parts[2],
                        "nonce": nonce,
                        "quotes": [
                            q
                            for q in quotes
                            if q.get("type") in {"DEFAULT", "PREMIUM"} and q.get("price") is not None
                        ],
                    }
                }
            )
            data = (await state.get_data())["relist"]
            rows = [
                [
                    (
                        ("🆓 Обычное" if q["type"] == "DEFAULT" else "⭐ PREMIUM") + f" · {q['price']} ₽",
                        f"relist:{nonce}:quote:{i}",
                    )
                ]
                for i, q in enumerate(data["quotes"])
            ]
            return (
                "🔄 <b>Выставить повторно</b>\n"
                + escaped(item.get("name"), 100)
                + f" · {escaped(item.get('price'))} ₽\nШтатное повторное размещение существующего товара. Создания копии нет. Тариф получен для этого проданного лота.\n"
                + self.automation(parts[2]),
                keyboard(
                    rows + [[("Изменить цену", "discount:" + parts[2])]],
                    back=("⬅️ К товару", "item:" + parts[2]),
                ),
            )
        data = (await state.get_data()).get("relist", {})
        if len(parts) < 3 or parts[1] != data.get("nonce"):
            raise Conflict("Подтверждение устарело. Откройте проданный товар заново")
        if (
            len(parts) == 4
            and parts[2] == "quote"
            and parts[3].isdigit()
            and int(parts[3]) < len(data["quotes"])
        ):
            quote = data["quotes"][int(parts[3])]
            value = await self.wizard.products.native_relist_preview(owner, data["id"], quote["id"])
            funds_note = ""
            if quote["type"] == "PREMIUM":
                from app.services.paid_promotions import placement_funds

                viewer = await self.api.call("get")
                if viewer.get("id") != value["account_id"]:
                    raise Conflict("Не подтверждён аккаунт баланса")
                available, pending, _ = placement_funds(viewer)
                funds_note = f"\nБаланс: {available} ₽ · заморозка дохода: {pending} ₽. Если баланса недостаточно, Playerok может использовать заморозку."
            nonce = secrets.token_hex(6)
            await state.update_data(relist={**data, "nonce": nonce, "value": value})
            return (
                "🔄 <b>Подтвердить повторное размещение?</b>\n"
                + ("⭐ PREMIUM" if quote["type"] == "PREMIUM" else "🆓 Обычное размещение")
                + f"\nИтого: {value.get('amount', 0)} ₽"
                + funds_note
                + "\nНажатие разрешает только это размещение. Настройки выдачи не включаются автоматически.",
                keyboard(
                    [[("✅ Подтвердить и выставить повторно", f"relist:{nonce}:commit")]],
                    back=("⬅️ К тарифам", "relist:item:" + data["id"]),
                ),
            )
        if parts[2] == "commit" and data.get("value"):
            operation = await self.wizard.products.queue_native_relist(
                owner, data["value"], "native-relist:" + data["nonce"]
            )
            await state.clear()
            return (
                "⏳ Повторное размещение ожидает подтверждения Playerok. Не повторяйте запрос при неизвестном результате.",
                keyboard([[("Результат и состояние выдачи", "result:show:" + operation)]]),
            )
        raise Conflict("Кнопка устарела. Обновите товар")

    def automation(self, ident):
        from app.services.product_readiness import ProductReadiness
        from app.telegram.product_presentation import brief

        return brief(ProductReadiness(self.store, self.api).snapshot(ident))

    async def status(self, owner, state, page=0):
        from app.telegram.listings_panel import ListingsPanel

        Automation(self.store, self.api).orders.owner(owner)
        await state.clear()
        await state.set_data({"item_filters": {"status": "APPROVED"}})
        return await ListingsPanel(self.api, self.store).show("items", state)
