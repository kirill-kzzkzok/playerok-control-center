import secrets
from urllib.parse import quote

from app.services.attachments import Attachments
from app.services.automation import Automation
from app.services.logical_products import LogicalProducts
from app.services.products import Products, ValidationError, priority_price_basis
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

    async def diagnostic(self, ident, owner, *, balance=False):
        from app.services.product_readiness import ProductReadiness
        from app.telegram.product_presentation import actions, summary

        result = await ProductReadiness(self.store, self.api).check(owner, ident)
        text = "🔍 <b>Проверка готовности</b>\n" + summary(result)
        return text, keyboard(
            actions(result) + [[("🔄 Проверить ещё раз", "rd:check:" + ident)]],
            back=("⬅️ К товару", "item:" + ident),
        )

    def permission_help(self, ident, owner):
        Automation(self.store, self.api).orders.owner(owner)
        return (
            (
                "🔐 <b>Разрешения запуска</b>\n"
                "Записи разрешает PLAYEROK_WRITES_ENABLED=true в локальном .env. "
                "Автоматику отдельно разрешает PLAYEROK_AUTOMATION_ENABLED=true; покупки подарков — TG_GIFT_PAYMENTS_ENABLED=true.\n"
                "Меняйте только нужные разрешения и перезапустите бота. В Telegram эти флаги не переключаются.\n"
                "После запуска нужна успешная проверка аккаунта. Разрешения не отменяют STOP, лимиты и сверку неизвестных операций."
            ),
            keyboard([], back=("⬅️ К товару", "item:" + ident)),
        )

    def history(self, ident, owner):
        from app.telegram.presentation import item_label, local_time, status

        logical = LogicalProducts(self.store, self.api)
        logical.authority(owner)
        product = logical.listing(ident)
        if product and product["owner_id"] != owner:
            raise ValidationError("История принадлежит другому владельцу")
        ids = (
            [
                r["item_id"]
                for r in self.store.rows(
                    "SELECT item_id FROM logical_listings WHERE product_id=?", (product["id"],)
                )
            ]
            if product
            else [ident]
        )
        marks = ",".join("?" for _ in ids)
        orders = self.store.rows(
            f"SELECT id,status,created_at FROM orders WHERE item_id IN ({marks}) ORDER BY created_at DESC LIMIT 5",
            ids,
        )
        rows = [
            [(local_time(o["created_at"]) + " · " + status(o["status"], "order"), "order:" + o["id"])]
            for o in orders
        ]
        operations = self.store.rows(
            f"SELECT b.id,b.status FROM outbox b LEFT JOIN orders o ON o.id=b.target WHERE o.item_id IN ({marks}) OR b.target IN ({marks}) ORDER BY b.created_at DESC LIMIT 5",
            ids + ids,
        )
        rows += [[("Операция: " + status(o["status"]), "ops:show:" + o["id"])] for o in operations]
        text = "📋 <b>Локальная история товара</b>\n" + escaped(item_label(self.store, ident), 200)
        if product:
            text += "\nКод товара: " + escaped(product["code"], 20)
        text += "\nПоследние заказы и операции. Текущее состояние объявления здесь не проверяется."
        if not orders and not operations:
            text += "\nИстория этого товара пока пуста."
        return text, keyboard(rows, back=("⬅️ К товарам", "items"))

    async def screen(self, ident, section, state, owner, gifts, stock):
        from app.telegram.presentation import status

        if section == "history":
            return self.history(ident, owner)
        item, details, photos = await self.card(ident, state)
        if isinstance(item.get("name"), str):
            self.store.set_setting("ui:item:" + ident + ":name", item["name"][:200])
        if section not in {
            "card",
            "edit",
            "delivery",
            "other",
            "details",
            "messages",
            "completion",
            "history",
        }:
            raise ValidationError("Экран устарел. Откройте товар заново")
        product = LogicalProducts(self.store, self.api).listing(ident)
        _, saved_policy = Automation(self.store, self.api).get(ident)
        platform = saved_policy.mode == "playerok"
        title = "📦 <b>" + escaped(item.get("name") or "Товар", 200) + "</b>"
        if section == "card":
            from app.services.product_readiness import ProductReadiness
            from app.telegram.product_presentation import summary

            readiness = ProductReadiness(self.store, self.api).snapshot(ident, item=item)
            text = (
                title
                + f"\nЦена: {escaped(item.get('price'), 40)} ₽\nСтатус: "
                + status(item.get("status"), "item")
            )
            if product:
                text += "\n🔐 Код товара: " + (
                    escaped(product["code"], 3) if len(product["code"]) == 3 else "требуется перенос"
                )
                text += "\nНаследовать настройки: " + (
                    "разрешено" if product["inherit_enabled"] else "выключено"
                )
            text += "\n" + summary(readiness)
            from app.database.store import now
            from app.services.finance import selected_fee_multiplier
            from app.services.withdrawals import draft_calculation

            cost = self.store.setting(f"finance:{owner}:cost:{ident}")
            estimate = (
                draft_calculation(
                    {
                        "price": item.get("price"),
                        "cost": cost,
                        "fee_multiplier": selected_fee_multiplier(
                            item.get("fee_multiplier"),
                            (item.get("obtaining_type") or {}).get("fee_multiplier"),
                        ),
                        "fee_checked_at": now(),
                    }
                )
                if item.get("price") is not None
                else None
            )
            text += "\nСебестоимость: " + (str(cost) + " ₽" if cost is not None else "не задана")
            if estimate is not None and estimate.after_platform is not None and estimate.cost is not None:
                text += f"\nДо вывода и размещения: ≈ {estimate.after_platform - estimate.cost} ₽"
            rows = [
                [("🔍 Проверить готовность", "rd:check:" + ident)],
                *(
                    [[("🔄 Обновить баланс", "rd:balance:" + ident)]]
                    if readiness["mode"] == "telegram_gifts"
                    else []
                ),
                [("⚙️ Настроить выдачу", "iv:delivery:" + ident)],
                [
                    ("💬 Сообщения покупателю", "iv:messages:" + ident),
                    ("✅ Завершение заказа", "iv:completion:" + ident),
                ],
                [("💰 Сколько получу чистыми", "finance:" + ident)],
                [("✏️ Редактировать", "iv:edit:" + ident), ("⭐ Продвижение", "payp:item:" + ident)],
                [("📋 История и операции", "iv:history:" + ident)],
                [("⋯ Другие действия", "iv:other:" + ident)],
            ]
            if item.get("status") == "DRAFT":
                rows.insert(0, [("🚀 Запустить продажи", "launch:item:" + ident)])
            elif item.get("status") == "SOLD":
                try:
                    basis = priority_price_basis(item)
                except ValidationError:
                    basis = None
                if item.get("may_be_published") is False:
                    text += "\nPlayerok не разрешил повторное размещение. Проверьте ограничения товара на площадке."
                elif item.get("may_be_published") is True and basis is not None:
                    rows.insert(0, [("🔄 Перевыставить", "relist:item:" + ident)])
                else:
                    text += (
                        "\nВозможность повторного размещения или его базовая цена не подтверждены сервером."
                    )
                    rows.insert(0, [("Проверить возможность перевыставления", "relist:item:" + ident)])
            elif product and not platform and not product["enabled"] and product["handler"] != "manual":
                rows.insert(0, [("🚀 Запустить продажи", "lp:ready:" + ident)])
        elif section == "edit":
            text = title + "\nЧто изменить?"
            rows = [
                [("Цена и скидка", "discount:" + ident)],
                [("Название", "editname:" + ident)],
                [("Описание", "editdesc:" + ident)],
                [("Фотографии", "editphoto:" + ident)],
                [("Характеристики и данные", "editfields:" + ident)],
            ] + photos
        elif section in {"delivery", "messages", "completion"}:
            from app.services.product_readiness import ProductReadiness
            from app.telegram.product_presentation import summary

            readiness = ProductReadiness(self.store, self.api).snapshot(ident, item=item)
            text = title + "\n" + summary(readiness)
            route = "platform:item:" + ident if platform else "lp:item:" + ident
            if section == "delivery":
                rows = [
                    [("⚙️ Настройки текущего способа", route)],
                    [("🔄 Изменить способ выдачи", "lp:method:" + ident)],
                    [("🔍 Проверить готовность", "rd:check:" + ident)],
                ]
                if readiness["mode"] == "unique_stock":
                    rows.insert(1, [("📚 Пополнить склад", "stock:view:" + ident)])
                if readiness["mode"] in {"unique_stock", "fixed_text"}:
                    rows.append([("Проверить конкретный заказ без отправки", "auto:check:" + ident)])
                if readiness["mode"] == "telegram_gifts":
                    rows[0] = [("🎁 Состав, сообщения и лимиты", route)]
                if not product:
                    rows.append([("📦 Выдаёт Playerok", "platform:item:" + ident)])
                for draft in stock.service.prepared(owner, ident):
                    rows.append([("📥 Перенести запас из мастера", "stock:transfer:" + draft)])
            elif section in {"messages", "completion"}:
                text += "\n\n" + (
                    "Сообщение после оплаты и отметка выполнения настраиваются независимо."
                    if platform
                    else "Приветствие отправляется до выдачи, итоговое сообщение — после подтверждения. У подарков действует стандартное уведомление, если своё не задано."
                )
                rows = [
                    [
                        (
                            "Изменить настройки",
                            "platform:item:" + ident if platform else "lp:" + section + ":" + ident,
                        )
                    ]
                ]
                if section == "messages":
                    rows.append([("Ответы на фразы покупателей — отдельно", "ar:menu")])
        elif section == "other":
            text = title + "\nДругие действия"
            rows = [
                [("Подробности", "iv:details:" + ident)],
            ]
            if item.get("status") == "DRAFT":
                rows.insert(0, [("🚦 Настроить и запустить", "launch:item:" + ident)])
            problem = Products(self.store, self.api).deletion_problem(ident) if self.store else None
            if problem:
                text += "\nУдаление заблокировано: " + escaped(problem["reason"])
                path = "order:" + problem["order_id"] if problem["order_id"] else "ops:list:ALL:0"
                rows.append([("Проверить обязательства", path)])
            else:
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

    async def edit_photos(self, action, owner, state):
        parts = action.split(":")
        if parts[0] == "editphoto":
            ident = parts[1]
            item = await self.owned(ident)
            if item.get("is_attachments_forbidden") is True:
                raise ValidationError("Playerok скрыл фотографии. Редактирование недоступно")
            ids = [image["id"] for image in self.images(item)]
            if len(ids) != len(item.get("attachments") or []) or len(set(ids)) != len(ids):
                raise ValidationError("Не удалось подтвердить полный набор фотографий")
            nonce = secrets.token_hex(6)
            view = {"id": ident, "ids": ids, "nonce": nonce, "owner": owner}
            await state.clear()
            await state.set_data(
                {"photo_edit": view, "item_gallery": {"id": ident, "images": ids, "nonce": nonce}}
            )
            rows = [[(f"Фото {i + 1}", f"ipe:{nonce}:select:{i}")] for i in range(len(ids))]
            rows.insert(0, [("➕ Добавить фото", f"ipe:{nonce}:add")])
            if ids:
                rows.append([("Заменить все фотографии…", f"ipe:{nonce}:all")])
            return (
                f"🖼 <b>Фотографии товара</b>\nСейчас: {len(ids)}. Выберите фото или добавьте новое.",
                keyboard(rows, back=("К редактированию", "iv:edit:" + ident)),
            )
        view = (await state.get_data()).get("photo_edit", {})
        if len(parts) not in {3, 4} or parts[1] != view.get("nonce") or owner != view.get("owner"):
            raise ValidationError("Выбор фотографии устарел. Откройте фотографии заново")
        item = await self.owned(view["id"])
        if [image["id"] for image in self.images(item)] != view["ids"]:
            raise ValidationError("Фотографии изменились на Playerok. Откройте их заново")
        command, prefix = parts[2], "ipe:" + view["nonce"] + ":"
        if command == "select":
            if len(parts) != 4 or not parts[3].isdigit() or not 0 <= int(parts[3]) < len(view["ids"]):
                raise ValidationError("Фотография недоступна")
            view["selected"] = int(parts[3])
            await state.update_data(photo_edit=view)
            return f"🖼 <b>Фото {view['selected'] + 1}</b>\nОстальные фотографии сохранятся.", keyboard(
                [
                    [("Просмотреть", f"itemmedia:{view['nonce']}:{view['selected']}")],
                    [("Заменить", prefix + "replace"), ("Удалить", prefix + "remove")],
                ],
                back=("Все фотографии", "editphoto:" + view["id"]),
            )
        if command not in {"add", "replace", "remove", "all"}:
            raise ValidationError("Действие с фотографией недоступно")
        selected = view.get("selected")
        if command in {"replace", "remove"} and (
            type(selected) is not int or not 0 <= selected < len(view["ids"])
        ):
            raise ValidationError("Сначала выберите фотографию")
        keep = list(view["ids"])
        if command == "all":
            keep = []
        elif command in {"replace", "remove"}:
            keep.pop(selected)
        if command == "remove":
            nonce = secrets.token_hex(8)
            await state.set_data(
                {
                    "pending": {
                        "kind": "edit",
                        "target": view["id"],
                        "payload": {"replace_attachment_ids": view["ids"], "keep_attachment_ids": keep},
                    },
                    "confirm_token": nonce,
                }
            )
            await state.set_state(None)
            return (
                f"Удалить только фото {selected + 1}? Остальные фотографии сохранятся. API не подтверждает порядок и главное фото; проверьте их на Playerok.",
                keyboard(
                    [[("✅ Удалить это фото", "commit:" + nonce)]], back=("Отмена", "editphoto:" + view["id"])
                ),
            )
        await state.set_data(
            {
                "item_id": view["id"],
                "attachment_ids": view["ids"],
                "keep_attachment_ids": keep,
                "photo_mode": command,
            }
        )
        await state.set_state("editphoto")
        return "Пришлите новое фото или файл JPEG/PNG/WEBP. " + (
            "После отдельного подтверждения будут заменены ВСЕ фотографии."
            if command == "all"
            else "Остальные фотографии сохранятся."
        ), keyboard([], back=("Отмена", "editphoto:" + view["id"]))

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
