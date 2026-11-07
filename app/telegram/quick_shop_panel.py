"""Small owner tools; all remote writes stay in the existing wizard/Outbox."""

import json
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

    async def status(self, owner):
        from app.playerok.adapter import validate_page
        from app.services.finance import quote_fresh
        from app.services.item_filters import catalog_item
        from app.services.product_readiness import ProductReadiness
        from app.telegram.listings_panel import ListingsPanel
        from playerokapi.enums import ItemStatuses

        Automation(self.store, self.api).orders.owner(owner)
        account = self.api.account.id
        bound = self.store.setting("playerok:account_id")
        if not account or bound not in (None, account):
            raise ValidationError("Аккаунт Playerok не подтверждён")
        cache = self.store.setting("ui:catalog:" + account, {}) if bound else {}
        if not cache and bound:
            saved = self.store.setting("ui:catalog_pages:" + account, {}).get(
                json.dumps(["APPROVED", None, {}, None], sort_keys=True)
            )
            if saved:
                items, cursor = validate_page(saved["page"], "items")
                if not cursor:
                    cache = {"items": items, "complete": True, "at": saved["at"]}
        if not cache:
            items, cursor, seen = {}, None, []
            for _ in range(3):
                page = await self.api.call(
                    "get_my_items", count=24, after_cursor=cursor, statuses=[ItemStatuses.APPROVED]
                )
                if self.api.account.id != account or self.store.setting("playerok:account_id") != bound:
                    raise ValidationError("Аккаунт Playerok изменился")
                values, cursor = validate_page(page, "items", seen)
                items.update((item["id"], catalog_item(item, account)) for item in values)
                if not cursor:
                    break
                seen.append(cursor)
            cache = {"items": list(items.values()), "complete": not cursor}
        if not cache.get("complete") or cache.get("at") and not quote_fresh(cache["at"], max_age=60):
            ListingsPanel(self.api, self.store).request_catalog(account)
        active = [
            item for item in cache["items"] if item["status"] == "APPROVED" and not item.get("deleted_at")
        ]
        text = f"📦 <b>Активные товары</b> · {len(active)}" + ("+" if not cache["complete"] else "")
        if cache.get("at") and not quote_fresh(cache["at"], max_age=60):
            text += " · сохранённый список"
        readiness = ProductReadiness(self.store, self.api)
        for index, item in enumerate(active, 1):
            result = readiness.snapshot(item["id"])
            delivery = (
                "Playerok"
                if result["mode"] == "playerok"
                else ("вкл" if result["ready"] else "пауза" if result["enabled"] else "выкл")
            )
            completion = "вкл" if result["enabled"] and result["complete"] else "выкл"
            reply = "вкл" if result["enabled"] and result["auto_message"] else "выкл"
            row = f"\n\n{index}. {escaped(item.get('name') or 'Товар', 90)}\nВыдача: {delivery} · Завершение: {completion} · Сообщение: {reply}"
            if result["mode"] == "playerok" and result["enabled"] and not result["ready"]:
                row += " · действия бота на паузе"
            if len((text + row).encode("utf-16-le")) > 7800:
                text += f"\n\nНе поместилось товаров: {len(active) - index + 1}."
                break
            text += row
        if not cache["complete"]:
            text += "\nСписок ещё обновляется."
        return text
