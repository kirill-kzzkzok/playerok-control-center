"""Durable product wizard; all remote mutations are delegated to outbox."""

import secrets
from html import escape

from aiogram.types import BufferedInputFile

from app.database.store import now
from app.services.attachments import Attachments
from app.services.finance import calculate, format_calculation, sale_rule_from_api
from app.services.products import ValidationError, listing_price, listing_text, validate_field
from app.telegram.formatting import escaped
from app.telegram.keyboards import keyboard


class Wizard:
    def __init__(self, store, api, products, attachments=None):
        self.store, self.api, self.products = store, api, products
        self.attachments = attachments or Attachments()

    async def start(self, owner, state):
        ident = self.store.draft(
            owner, {"step": "game", "fields": {}, "photos": [], "delivery": "manual", "auto_confirm": False}
        )
        await state.set_state("wizard")
        await state.set_data({"draft_id": ident, "revision": 0})
        return await self.screen(owner, state)

    async def save(self, owner, state, payload):
        data = await state.get_data()
        self.store.update_draft(data["draft_id"], owner, data["revision"], payload)
        await state.update_data(revision=data["revision"] + 1)

    async def load_form(self, payload, obtaining_type_id):
        snapshot = await self.products.schema_details(payload["category_id"], obtaining_type_id)
        payload["form"] = snapshot["form"]
        payload["fee_multiplier"] = snapshot["fee_multiplier"]
        payload["category_fee_multiplier"] = snapshot["category_fee_multiplier"]
        payload["obtaining_fee_multiplier"] = snapshot["obtaining_fee_multiplier"]
        payload["fee_checked_at"] = snapshot["checked_at"]
        payload["fee_source"] = "https://playerok.com/sell; live GamePageCategory/gameCategoryObtainingTypes"
        payload["field_index"] = 0

    async def screen(self, owner, state):
        data = await state.get_data()
        draft = self.store.get_draft(data["draft_id"], owner)
        payload = draft["payload"]
        nonce = secrets.token_hex(3)
        await state.update_data(view_token=nonce, revision=draft["revision"])
        rows, choices = [], []
        step = payload.get("step", "game")
        text = ""
        if step == "game":
            page = await self.api.call(
                "get_games", name=payload.get("search"), after_cursor=payload.get("cursor"), count=12
            )
            choices = page["games"]
            text = "➕ <b>Новый товар · Игра</b>\nВыберите игру или введите название для поиска."
            if (page.get("page_info") or {}).get("has_next_page"):
                await state.update_data(next_cursor=page["page_info"]["end_cursor"])
                rows.append([("Далее →", f"w:{nonce}:next")])
        elif step == "category":
            game = await self.api.call("get_game", id=payload["game_id"])
            choices = game.get("categories") or []
            text = "📂 <b>Категория</b>\nДоступные категории Playerok:"
        elif step == "obtaining":
            async for page in self.api.pages(
                "get_game_category_obtaining_types",
                "obtaining_types",
                game_category_id=payload["category_id"],
            ):
                choices.extend(page)
            if not choices:
                payload["obtaining_type_id"] = None
                payload["obtaining_not_required"] = True
                await self.load_form(payload, None)
                payload["step"] = "fields"
                await self.save(owner, state, payload)
                return await self.screen(owner, state)
            text = "📬 <b>Способ получения</b>\nВыберите вариант Playerok."
        elif step == "fields":
            form = payload["form"]
            index = payload.get("field_index", 0)
            if index >= len(form):
                payload["step"] = "name"
                await self.save(owner, state, payload)
                return await self.screen(owner, state)
            field = form[index]
            text = f"🧩 <b>{escape(field['label'])}</b>\n" + (
                "Обязательное поле" if field["required"] else "Необязательное: введите − для пропуска"
            )
            if field["kind"] in {"choice", "multi"}:
                selected = payload.get("fields", {}).get(field["key"], []) if field["kind"] == "multi" else []
                choices = [
                    {
                        "name": ("✅ " if o["value"] in selected else "☐ ")
                        + (o.get("label") or str(o["value"]))
                        if field["kind"] == "multi"
                        else o.get("label") or str(o["value"]),
                        "value": o["value"],
                    }
                    for o in field["options"]
                ]
                if field["kind"] == "multi":
                    text += "\nМожно выбрать несколько значений. Затем нажмите «Готово»."
                    rows.append([("✅ Готово", f"w:{nonce}:field_done")])
        elif step == "name":
            text = "📝 <b>Название</b>\nВведите название от 10 до 72 символов по текущим правилам формы Playerok."
        elif step == "description":
            text = "📋 <b>Описание</b>\nВведите многострочный текст от 10 до 2048 символов."
        elif step == "photos":
            text = f"🖼 <b>Фотографии: {len(payload['photos'])}</b>\nПришлите фото или файл JPEG/PNG/WEBP до 10 МБ. Локальный предел — 10 фото; лимиты API ещё не подтверждены."
            rows = [
                [("Готово → Цена", f"w:{nonce}:photos_done")],
            ]
            replacement = payload.get("replace_photo_index")
            if replacement is not None:
                text += f"\n\nПришлите новое изображение вместо фото №{replacement + 1}."
            for i in range(len(payload["photos"])):
                rows.extend(
                    [
                        [
                            (f"🖼 Фото {i + 1}", f"w:{nonce}:photo_preview:{i}"),
                            ("Заменить", f"w:{nonce}:photo_replace:{i}"),
                            ("Удалить", f"w:{nonce}:photo_remove:{i}"),
                        ]
                    ]
                )
        elif step == "price":
            text = "💰 <b>Цена продажи</b>\nВведите целую сумму в рублях. Установка настоящей скидки пока недоступна: протокол не подтверждён."
        elif step == "delivery":
            text = "⚡ <b>Выдача</b>\nВыберите локальный режим. Автовыдача остаётся выключенной до отдельного включения после проверки."
            choices = [
                {"name": "Ручная", "value": "manual"},
                {"name": "Постоянный текст", "value": "text"},
                {"name": "Уникальные значения", "value": "stock"},
            ]
        elif step == "delivery_data":
            text = "🔐 Пришлите текст выдачи или уникальные значения (одно на строку). Данные сохраняются только в локальной БД."
        elif step == "confirm":
            text = "✅ <b>Подтверждение сделки</b>\nАвтоматическое SENT выключено. SENT продавца и подтверждение покупателя — разные действия."
            rows = [[("Продолжить", f"w:{nonce}:confirm_done")]]
        elif step == "preview":
            rule = sale_rule_from_api(payload.get("fee_multiplier"), payload.get("fee_checked_at") or now())
            calculation = format_calculation(calculate(payload["price"], platform=rule))
            text = (
                f"📦 <b>{escape(payload.get('name', ''))}</b>\n\n{escaped(payload.get('description', ''), 2200)}\n\n"
                f"💰 Цена: {payload.get('price')} ₽\n🖼 Фото: {len(payload['photos'])}\n"
                f"⚡ Выдача: {escape(payload['delivery'])} (автоматика выключена)\n\n"
                f"{escape(calculation)}\n"
                "Стоимость PREMIUM доступна только после создания серверного черновика. Покупка запрещена.\n"
                "Первое действие создаёт черновик на Playerok. Публикация отдельная, только при подтверждённой нулевой стоимости."
            )
            rows = [
                [("Создать серверный черновик", f"w:{nonce}:submit")],
                [("📝 Название", f"w:{nonce}:edit:name"), ("📋 Описание", f"w:{nonce}:edit:description")],
                [("🖼 Фото", f"w:{nonce}:edit:photos"), ("💰 Цена", f"w:{nonce}:edit:price")],
                [("⚡ Выдача", f"w:{nonce}:edit:delivery")],
            ]
        choice_view = f"{step}:{payload.get('category_id', '')}:{payload.get('field_index', 0)}"
        if choices:
            page_info = payload.get("choice_page") or {}
            page_number = page_info.get("number", 0) if page_info.get("view") == choice_view else 0
            page_count = (len(choices) + 11) // 12
            page_number = min(max(0, page_number), page_count - 1)
            await state.update_data(choice_view=choice_view, choice_pages=page_count)
            choices = choices[page_number * 12 : (page_number + 1) * 12]
            navigation = []
            if page_number:
                navigation.append(("← Варианты", f"w:{nonce}:choice_page:{page_number - 1}"))
            if page_number + 1 < page_count:
                navigation.append(("Варианты →", f"w:{nonce}:choice_page:{page_number + 1}"))
            if navigation:
                rows.append(navigation)
                text += f"\nСтраница вариантов: {page_number + 1}/{page_count}"
            rows = [
                [
                    (
                        (choice.get("name") or choice.get("label") or str(choice.get("id")))[:60],
                        f"w:{nonce}:choose:{i}",
                    )
                ]
                for i, choice in enumerate(choices)
            ] + rows
        await state.update_data(choices=choices)
        if step not in {"game", "preview"}:
            rows.append([("⬅️ Назад", f"w:{nonce}:back")])
        rows.append([("💾 Сохранить и выйти", "home"), ("❌ Отмена", "cancel_wizard")])
        return text, keyboard(rows)

    async def callback(self, owner, state, callback, message=None):
        data = await state.get_data()
        parts = callback.split(":")
        if len(parts) < 3 or parts[1] != data.get("view_token"):
            raise ValidationError("Кнопка устарела. Откройте черновик заново.")
        draft = self.store.get_draft(data["draft_id"], owner)
        payload, action = draft["payload"], parts[2]
        step = payload["step"]
        removed_photo = None
        if action == "choice_page":
            try:
                number = int(parts[3])
                if not 0 <= number < data.get("choice_pages", 0):
                    raise ValueError
            except (ValueError, IndexError):
                raise ValidationError("Страница вариантов недоступна") from None
            payload["choice_page"] = {"view": data["choice_view"], "number": number}
        elif action == "back":
            if step == "fields" and payload.get("field_index", 0) > 0:
                payload["field_index"] -= 1
            else:
                previous = {
                    "category": "game",
                    "obtaining": "category",
                    "fields": "obtaining",
                    "name": "fields",
                    "description": "name",
                    "photos": "description",
                    "price": "photos",
                    "delivery": "price",
                    "delivery_data": "delivery",
                    "confirm": "delivery",
                }
                if step not in previous:
                    raise ValidationError("Предыдущий шаг недоступен")
                payload["step"] = previous[step]
                if step in {"name", "fields"} and (not payload.get("form") or step == "fields"):
                    payload["step"] = "category" if payload.get("obtaining_not_required") else "obtaining"
                if payload["step"] == "fields":
                    payload["field_index"] = max(0, len(payload.get("form", [])) - 1)
        elif action == "next" and step == "game":
            payload["cursor"] = data.get("next_cursor")
        elif action == "choose":
            try:
                index = int(parts[3])
                if not 0 <= index < len(data["choices"]):
                    raise ValueError
                choice = data["choices"][index]
            except (ValueError, IndexError, KeyError):
                raise ValidationError("Некорректный выбор") from None
            if step in {"game", "category", "obtaining"}:
                key, next_step = {
                    "game": ("game_id", "category"),
                    "category": ("category_id", "obtaining"),
                    "obtaining": ("obtaining_type_id", "fields"),
                }[step]
                if step in {"game", "category"}:
                    for dependent in (
                        "obtaining_type_id",
                        "obtaining_not_required",
                        "form",
                        "field_index",
                        "fee_multiplier",
                        "fee_checked_at",
                    ):
                        payload.pop(dependent, None)
                    payload["fields"] = {}
                    if step == "game":
                        payload.pop("category_id", None)
                payload[key], payload["step"] = choice["id"], next_step
                if step == "obtaining":
                    payload["obtaining_not_required"] = False
                    await self.load_form(payload, choice["id"])
            elif step == "fields":
                field = payload["form"][payload["field_index"]]
                if field["kind"] == "multi":
                    selected = list(payload["fields"].get(field["key"], []))
                    if choice["value"] in selected:
                        selected.remove(choice["value"])
                    else:
                        selected.append(choice["value"])
                    payload["fields"][field["key"]] = selected
                else:
                    payload["fields"][field["key"]] = validate_field(field, choice["value"])
                    payload["field_index"] += 1
            elif step == "delivery":
                payload["delivery"] = choice["value"]
                payload["step"] = "confirm" if choice["value"] == "manual" else "delivery_data"
            else:
                raise ValidationError("Выбор не соответствует шагу")
        elif action == "field_done" and step == "fields":
            field = payload["form"][payload["field_index"]]
            if field["kind"] != "multi":
                raise ValidationError("Подтверждение не соответствует текущему полю")
            payload["fields"][field["key"]] = validate_field(field, payload["fields"].get(field["key"], []))
            payload["field_index"] += 1
        elif action == "photos_done" and step == "photos":
            payload.pop("replace_photo_index", None)
            payload["step"] = "price"
        elif action in {"photo_remove", "photo_replace", "photo_preview"} and step == "photos":
            try:
                index = int(parts[3])
                if not 0 <= index < len(payload["photos"]):
                    raise ValueError
            except (ValueError, IndexError):
                raise ValidationError("Фотография недоступна; обновите черновик") from None
            if action == "photo_preview":
                if message is None:
                    raise ValidationError("Предпросмотр доступен из сообщения бота")
                image = await self.attachments.read(payload["photos"][index])
                await message.answer_photo(
                    BufferedInputFile(image, filename="preview.jpg"), caption=f"Фото {index + 1}"
                )
                return await self.screen(owner, state)
            if action == "photo_replace":
                payload["replace_photo_index"] = index
            else:
                removed_photo = payload["photos"].pop(index)
                # Validate cleanup scope before committing a changed draft.
                self.attachments.path(removed_photo)
                payload.pop("replace_photo_index", None)
        elif action == "confirm_done" and step == "confirm":
            payload["step"] = "preview"
        elif action == "edit" and step == "preview":
            target = parts[3] if len(parts) > 3 else "name"
            if target not in {"name", "description", "photos", "price", "delivery"}:
                raise ValidationError("Неизвестное поле редактирования")
            payload["step"] = target
        elif action == "submit" and step == "preview":
            if not self.api.account.writes_enabled:
                raise ValidationError("Записи Playerok отключены в .env. Локальный черновик сохранён.")
            ident = await self.products.queue_create(draft["id"], owner, draft["revision"])
            await state.clear()
            return f"Операция сохранена: <code>{ident}</code>. Результат смотрите в диагностике.", keyboard(
                []
            )
        else:
            raise ValidationError("Действие не соответствует шагу")
        await self.save(owner, state, payload)
        if removed_photo:
            self.attachments.discard(removed_photo)
        return await self.screen(owner, state)

    async def message(self, message, state):
        owner = message.from_user.id
        data = await state.get_data()
        draft = self.store.get_draft(data["draft_id"], owner)
        payload, text = draft["payload"], message.text or ""
        step = payload["step"]
        new_photo = None
        replaced_photo = None
        if step == "game":
            payload["search"], payload["cursor"] = text[:100], None
        elif step == "fields":
            field = payload["form"][payload["field_index"]]
            payload["fields"][field["key"]] = validate_field(field, "" if text == "−" else text)
            payload["field_index"] += 1
        elif step in {"name", "description"}:
            payload[step] = listing_text(text, step)
            payload["step"] = "description" if step == "name" else "photos"
        elif step == "photos":
            file = message.photo[-1] if message.photo else message.document
            replacement = payload.get("replace_photo_index")
            if (
                not file
                or (file.file_size or 0) > 10_000_000
                or (len(payload["photos"]) >= 10 and replacement is None)
            ):
                raise ValidationError("Ожидается фото/изображение до 10 МБ; максимум 10 файлов локально")
            if replacement is not None and not 0 <= replacement < len(payload["photos"]):
                raise ValidationError("Заменяемая фотография недоступна; обновите черновик")
            new_photo = await self.attachments.download(message.bot, file)
            if replacement is not None:
                replaced_photo = payload["photos"][replacement]
                self.attachments.path(replaced_photo)
                payload["photos"][replacement] = new_photo
                payload.pop("replace_photo_index", None)
            else:
                payload["photos"].append(new_photo)
        elif step == "price":
            payload["price"] = listing_price(text, getattr(self.api.account, "role", None))
            payload["step"] = "delivery"
        elif step == "delivery_data":
            if not text.strip() or len(text) > 10000:
                raise ValidationError("Введите непустые данные до 10000 символов")
            payload["delivery_data"] = text
            payload["step"] = "confirm"
        else:
            raise ValidationError("На этом шаге используйте кнопки")
        try:
            await self.save(owner, state, payload)
        except BaseException:
            if new_photo:
                self.attachments.discard(new_photo)
            raise
        if replaced_photo:
            self.attachments.discard(replaced_photo)
        return await self.screen(owner, state)
