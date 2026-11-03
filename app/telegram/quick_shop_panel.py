"""Small owner tools; all remote writes stay in the existing wizard/Outbox."""

import asyncio
import secrets

from app.database.store import Conflict
from app.playerok.adapter import validate_page
from app.services.automation import Automation
from app.services.logical_products import LogicalProducts
from app.services.products import ValidationError, priority_price_basis
from app.telegram.formatting import escaped
from app.telegram.keyboards import keyboard
from playerokapi.enums import ItemStatuses


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
        from app.telegram.product_presentation import summary

        return summary(ProductReadiness(self.store, self.api).snapshot(ident), compact=True)

    async def status(self, owner, state, page=0):
        LogicalProducts(self.store, self.api).authority(owner)
        data = await state.get_data()
        cursors = data.get("status_cursors", [None]) if page else [None]
        if not 0 <= page < len(cursors):
            raise ValidationError("Страница устарела. Отправьте /status заново")
        result = await self.api.call(
            "get_my_items", statuses=[ItemStatuses.APPROVED], count=5, after_cursor=cursors[page]
        )
        values, next_cursor = validate_page(result, "items", [c for c in cursors[:page] if c])
        from app.services.product_readiness import ProductReadiness
        from app.telegram.product_presentation import summary

        readiness = ProductReadiness(self.store, self.api)
        lines, item_rows = [], []

        async def complete(value):
            if all(key in value for key in ("status", "name", "price", "user")):
                return value
            return await Automation(self.store, self.api).item(owner, value["id"])

        items = await asyncio.gather(*(complete(v) for v in values[:5]))
        for item in items:
            if (item.get("user") or {}).get("id") != self.api.account.id:
                continue
            if item.get("status") != "APPROVED" or item.get("deleted_at"):
                continue
            priority = item.get("priority")
            if isinstance(priority, dict):
                priority = priority.get("type")
            premium = "🟢" if priority == "PREMIUM" else "🔴" if priority == "DEFAULT" else "⚪ неизвестно"
            lines.append(
                str(len(lines) + 1)
                + ". 📦 <b>"
                + escaped(item.get("name"), 65)
                + "</b> — "
                + escaped(item.get("price"), 20)
                + " ₽\n"
                + summary(readiness.snapshot(item["id"], item=item), compact=True)
                + "\n⭐ PREMIUM: "
                + premium
            )
            number = len(lines)
            item_rows.append(
                [
                    (f"🔍 {number}. Проверить", "rd:check:" + item["id"]),
                    (f"⚙️ {number}. Открыть товар", "item:" + item["id"]),
                ]
            )
        title = (
            "📊 <b>МОИ АКТИВНЫЕ ТОВАРЫ"
            + (f" — {len(lines)}" if not next_cursor and page == 0 else f" · страница {page + 1}")
            + "</b>"
        )
        if next_cursor:
            cursors = cursors[: page + 1] + [next_cursor]
        await state.update_data(status_cursors=cursors)
        rows = []
        if page:
            rows.append(("← Назад", f"status:view:{page - 1}"))
        if next_cursor:
            rows.append(("Далее →", f"status:view:{page + 1}"))
        return title + "\n\n" + (
            "\n\n".join(lines) or "Подтверждённых активных товаров на этой странице нет."
        ), keyboard(
            item_rows + ([rows] if rows else []) + [[("🔄 Обновить", "status:view:0")]],
            back=("⬅️ Мой магазин", "home"),
        )
