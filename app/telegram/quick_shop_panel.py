"""Small owner tools; all remote writes stay in the existing wizard/Outbox."""

import json
import secrets
from datetime import UTC, datetime

from app.database.store import Conflict
from app.playerok.adapter import validate_page
from app.services.automation import Automation
from app.services.logical_products import LogicalProducts
from app.services.products import ValidationError
from app.telegram.formatting import escaped
from app.telegram.keyboards import keyboard
from app.telegram.presentation import recent
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
            quotes = await self.wizard.products.priority_preview(parts[2], item["raw_price"])
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
        automation = Automation(self.store, self.api)
        _, policy = automation.get(ident)
        gift = self.gifts.listing(ident)
        product = LogicalProducts(self.store, self.api).listing(ident)
        if policy.mode == "playerok":
            gift = None
        enabled = bool(gift["enabled"]) if gift else policy.enabled
        mode = (
            "Telegram Gifts"
            if gift
            else {
                "playerok": "Playerok",
                "text": "Постоянный текст",
                "stock": "Склад",
                "manual": "Не настроена",
            }[policy.mode]
        )
        reply = (
            bool(self.store.setting("gifts:listing_options:" + ident, {}).get("after"))
            if gift
            else bool(policy.paid_reply if policy.mode == "playerok" else policy.greeting or policy.after)
        )
        complete = bool(gift["auto_fulfill"]) if gift else policy.mark_sent
        ready, unknown = False, False
        if enabled:
            if gift:
                ready = "Подарочная выдача: Работает" in self.gifts.status_text(ident)
                balance = self.store.setting("gifts:balance", {})
                unknown = not recent(balance.get("at")) or not recent(
                    self.store.setting("gifts:connection", {}).get("at")
                )
                try:
                    ready = (
                        ready
                        and self.gifts.catalog_bundle(gift["gift_code"]) == json.loads(gift["bundle"])
                        and balance.get("stars", -1) >= gift["approved_stars"]
                    )
                    order_limit, day_limit = self.gifts.limits(ident)
                    reserved = self.store.rows("SELECT COALESCE(SUM(reserved_stars),0) AS n FROM gift_jobs")[
                        0
                    ]["n"]
                    spent = self.store.rows(
                        "SELECT COALESCE(SUM(stars),0) AS n FROM gift_steps WHERE spent_at>=?",
                        (datetime.now(UTC).date().isoformat(),),
                    )[0]["n"]
                    ready = (
                        ready
                        and gift["approved_stars"] <= order_limit
                        and gift["approved_stars"] + reserved + spent <= day_limit
                        and gift["approved_stars"] + reserved <= balance.get("stars", -1)
                    )
                    unknown = unknown or bool(
                        self.store.rows(
                            "SELECT 1 FROM gift_steps WHERE spent_at>? LIMIT 1", (balance.get("at", ""),)
                        )
                    )
                except ValueError:
                    ready = False
            else:
                ready = automation.readiness(ident)["status"] == "Работает"
            if product and (not product["enabled"] or product["state"] != "VERIFIED"):
                ready = False

        def flag(on):
            return (
                "🔴 выключено"
                if not on or not enabled
                else "🟡 включено, не готово"
                if not ready
                else "⚪ нет свежей проверки"
                if unknown
                else "🟢 готово"
            )

        return f"📦 Выдача: {mode} · {flag(enabled)}\n💬 Автоответ{' после выдачи' if gift else ''}: {flag(reply)}\n✅ Автовыполнение: {flag(complete)}"

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
        lines = []
        for value in values[:5]:
            item = await Automation(self.store, self.api).item(owner, value["id"])
            if item.get("status") != "APPROVED" or item.get("deleted_at"):
                continue
            priority = item.get("priority")
            if isinstance(priority, dict):
                priority = priority.get("type")
            premium = "🟢" if priority == "PREMIUM" else "🔴" if priority == "DEFAULT" else "⚪ неизвестно"
            lines.append(
                "📦 <b>"
                + escaped(item.get("name"), 65)
                + "</b> — "
                + escaped(item.get("price"), 20)
                + " ₽\n"
                + self.automation(item["id"])
                + "\n⭐ PREMIUM: "
                + premium
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
            ([rows] if rows else []) + [[("🔄 Обновить", "status:view:0")]], back=("⬅️ Мой магазин", "home")
        )
