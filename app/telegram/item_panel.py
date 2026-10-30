import secrets
from urllib.parse import quote

from app.services.attachments import Attachments
from app.services.products import ValidationError
from app.telegram.formatting import escaped
from app.telegram.keyboards import keyboard


def counter(value):
    return str(value) if type(value) is int and value >= 0 else "нет данных"


class ItemPanel:
    def __init__(self, api):
        self.api = api
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
