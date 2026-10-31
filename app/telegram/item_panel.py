import secrets
from urllib.parse import quote

from app.services.attachments import Attachments
from app.services.automation import Automation
from app.services.products import ValidationError
from app.telegram.formatting import escaped
from app.telegram.keyboards import keyboard


def counter(value):
    return str(value) if type(value) is int and value >= 0 else "нет данных"


class ItemPanel:
    def __init__(self, api, store=None):
        self.api, self.store = api, store
        self.attachments = Attachments()

    async def owned(self, ident):
        item = await self.api.call("get_item", id=ident)
        if (
            not self.api.account.id
            or item.get("id") != ident
            or (item.get("user") or {}).get("id") != self.api.account.id
        ):
            raise ValidationError("Владение лотом не подтверждено")
        return item

    def images(self, item):
        if item.get("is_attachments_forbidden") is True:
            return []
        return [
            image
            for image in (item.get("attachments") or [])
            if isinstance(image, dict) and isinstance(image.get("id"), str) and image["id"]
        ]

    async def card(self, ident, state):
        item = await self.owned(ident)
        images = self.images(item)
        if len(images) > 100:
            raise ValidationError("Слишком много изображений в ответе Playerok")
        nonce = secrets.token_hex(6)
        await state.update_data(
            item_gallery={"id": ident, "images": [image["id"] for image in images], "nonce": nonce}
        )
        text = (
            f"📦 <b>{escaped(item.get('name') or ident, 200)}</b>\nЦена: {escaped(item.get('price'), 40)} ₽"
            + f"\nСтатус: {escaped(item.get('status') or 'нет данных', 80)}"
            + f"\nПриоритет: {escaped(item.get('priority') or 'нет данных', 80)}"
            + f"\nПозиция внутри приоритета: {counter(item.get('priority_position'))}"
            + f"\nПросмотры по API: {counter(item.get('views_counter'))}"
            + f"\nСделки по API: {counter(item.get('deals_counter'))}"
            + "\nСчётчик сделок не доказывает число завершённых продаж."
            + (
                "\nФото скрыты Playerok."
                if item.get("is_attachments_forbidden") is True
                else f"\nДоступных фото: {len(images)}"
            )
            + "\n\n"
            + escaped(item.get("description") or "", 1400)
            + '\n\n<a href="https://playerok.com/products/'
            + quote(str(item.get("slug") or ident), safe="")
            + '">Открыть на Playerok</a>'
        )
        rows = [[("🖼 Просмотреть фото", f"itemmedia:{nonce}:0")]] if images else []
        return item, text, rows

    async def screen(self, ident, section, state, owner, gifts, stock):
        from app.telegram.presentation import automation_summary, status

        item, details, photos = await self.card(ident, state)
        if isinstance(item.get("name"), str):
            self.store.set_setting("ui:item:" + ident + ":name", item["name"][:200])
        if section not in {"card", "edit", "delivery", "other", "details"}:
            raise ValidationError("Экран устарел. Откройте товар заново")
        binding = gifts.service.listing(ident)
        policy = self.store.rows("SELECT 1 FROM delivery_policies WHERE item_id=?", (ident,))
        gift_applicable = binding or (
            (item.get("category") or {}).get("id") == gifts.service.settings.category_id
            and (item.get("obtaining_type") or {}).get("id") == gifts.service.settings.obtaining_type_id
        )
        title = "📦 <b>" + escaped(item.get("name") or "Товар", 200) + "</b>"
        if section == "card":
            delivery = (
                ("настроена для товара; проверьте готовность" if binding["enabled"] else "выключена")
                if binding
                else (
                    Automation(self.store, self.api).readiness(ident)["status"].lower()
                    if policy
                    else "не настроена"
                )
            )
            text = (
                title
                + f"\nЦена: {escaped(item.get('price'), 40)} ₽"
                + "\nСтатус: "
                + status(item.get("status"), "item")
                + "\nВыдача: "
                + delivery
            )
            rows = [
                [("✏️ Редактировать", "iv:edit:" + ident)],
                [("⚡ Настроить выдачу", "iv:delivery:" + ident)],
                [("⋯ Другие действия", "iv:other:" + ident)],
            ]
        elif section == "edit":
            text = title + "\nЧто изменить?"
            rows = [
                [("Цена и скидка", "discount:" + ident)],
                [("Название", "editname:" + ident)],
                [("Описание", "editdesc:" + ident)],
                [("Фотографии", "editphoto:" + ident)],
                [("Характеристики и данные", "editfields:" + ident)],
            ] + photos
        elif section == "delivery":
            text = (
                title
                + "\n"
                + (gifts.summary(ident) if binding else automation_summary(self.store, self.api, ident))
            )
            if binding:
                rows = [[("🎁 Настроить подарки", "gft:item:" + ident)]]
                # Do not advertise competing fulfillment while a gift binding is active.
                if not binding["enabled"]:
                    rows.append([("Другой способ выдачи", "auto:item:" + ident)])
            else:
                rows = [
                    [("📚 Склад", "stock:view:" + ident)],
                    [("Настроить автовыдачу / проверить без отправки", "auto:item:" + ident)],
                ]
                if gift_applicable:
                    rows.append([("🎁 Настроить подарки", "gft:item:" + ident)])
            for draft in stock.service.prepared(owner, ident):
                rows.append([("📥 Перенести запас из мастера", "stock:transfer:" + draft)])
        elif section == "other":
            text = title + "\nДругие действия"
            rows = [
                [("Финансовый расчёт", "finance:" + ident)],
                [("Продвижение — проверка", "promo:item:" + ident)],
                [("Стоимость размещения", "quote:" + ident)],
                [("Подробности", "iv:details:" + ident)],
            ]
            if item.get("status") == "DRAFT":
                rows.append([("Опубликовать бесплатно · проверка", "publish:" + ident)])
            rows.append([("🗑 Удалить", "delete:" + ident)])
        else:
            text, rows = details, photos
        markup = keyboard(
            rows, back=("⬅️ К товарам", "items") if section == "card" else ("⬅️ К товару", "item:" + ident)
        )
        if section == "other":
            from aiogram.types import InlineKeyboardButton

            markup.inline_keyboard.insert(
                0,
                [
                    InlineKeyboardButton(
                        text="Открыть на Playerok",
                        url="https://playerok.com/products/" + quote(str(item.get("slug") or ident), safe=""),
                    )
                ],
            )
        return text, markup

    async def media(self, action, state):
        parts = action.split(":")
        view = (await state.get_data()).get("item_gallery", {})
        if len(parts) != 3 or parts[1] != view.get("nonce"):
            raise ValidationError("Просмотр фото устарел. Откройте лот заново")
        try:
            index = int(parts[2])
            if not 0 <= index < len(view["images"]):
                raise ValueError
            image_id = view["images"][index]
        except (ValueError, KeyError, TypeError):
            raise ValidationError("Фотография недоступна") from None
        item = await self.owned(view["id"])
        selected = next((image for image in self.images(item) if image["id"] == image_id), None)
        if not selected:
            raise ValidationError("Фотография скрыта или изменилась. Откройте лот заново")
        content = await self.attachments.remote(selected.get("url"))
        navigation = []
        if index:
            navigation.append(("← Фото", f"itemmedia:{view['nonce']}:{index - 1}"))
        if index + 1 < len(view["images"]):
            navigation.append(("Фото →", f"itemmedia:{view['nonce']}:{index + 1}"))
        rows = [navigation] if navigation else []
        rows.append([("📦 Лот", "item:" + view["id"])])
        return (
            content,
            f"🖼 Фото {index + 1} из {len(view['images'])}\n" + escaped(item.get("name") or "", 150),
            keyboard(rows),
        )
