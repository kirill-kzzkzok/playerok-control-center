"""Durable product wizard; all remote mutations are delegated to outbox."""

import secrets
from html import escape

from aiogram.types import BufferedInputFile

from app.services.agreements import fingerprint, require_conditions
from app.services.attachments import Attachments
from app.services.finance import format_calculation, money
from app.services.financial_quotes import save_quote
from app.services.inventory import stock_values
from app.services.product_codes import title_code, titled
from app.services.products import (
    ValidationError,
    listing_price,
    listing_text,
    text_units,
    validate_draft,
    validate_field,
)
from app.services.withdrawals import draft_calculation, withdrawal_methods, withdrawal_snapshot
from app.telegram.formatting import escaped, html_pages
from app.telegram.keyboards import keyboard
from app.telegram.storage import replace_context


class Wizard:
    @staticmethod
    def gift_description(description, code, max_count):
        from app.gifts.core import gift_code_from_description

        previous = gift_code_from_description(description, required=False, max_count=max_count)
        if previous is None:
            return description
        if previous != code:
            raise ValidationError(
                "Состав в старой строке не совпадает с выбранными подарками. Измените выбор подарков"
            )
        # The strict parser proved this is a single final metadata line.
        lines = description.rstrip().splitlines()
        return listing_text("\n".join(lines[:-1]).rstrip(), "description")

    def validate_gift(self, payload):
        from app.gifts.core import gift_code_from_description
        from app.gifts.settings import GiftSettings

        settings = getattr(self.api, "gift_settings", None) or GiftSettings.load()
        if (
            settings.category_id
            and settings.obtaining_type_id
            and payload.get("category_id") == settings.category_id
            and payload.get("obtaining_type_id") == settings.obtaining_type_id
        ):
            if payload.get("managed_delivery") and payload.get("gift_code"):
                gift_code_from_description("gift: " + payload["gift_code"], max_count=settings.max_gifts)
                return
            gift_code_from_description(payload.get("description", ""), max_count=settings.max_gifts)

    def __init__(self, store, api, products, attachments=None):
        self.store, self.api, self.products = store, api, products
        self.attachments = attachments or Attachments()

    async def start(self, owner, state):
        ident = self.store.draft(
            owner,
            {
                "step": "game",
                "fields": {},
                "photos": [],
                "delivery": "manual",
                "auto_confirm": False,
                "withdrawal": self.store.setting(f"finance:{owner}:withdrawal"),
            },
        )
        await replace_context(state, "wizard", {"draft_id": ident, "revision": 0})
        return await self.screen(owner, state)

    async def start_edit(self, owner, state, item_id):
        ident = await self.products.edit_fields_draft(owner, item_id)
        draft = self.store.get_draft(ident, owner)
        await self.load_form(draft["payload"], draft["payload"]["obtaining_type_id"])
        self.store.update_draft(ident, owner, 0, draft["payload"])
        await replace_context(state, "wizard", {"draft_id": ident, "revision": 1})
        return await self.screen(owner, state)

    async def save(self, owner, state, payload):
        data = await state.get_data()
        self.store.update_draft(data["draft_id"], owner, data["revision"], payload)
        await state.update_data(revision=data["revision"] + 1)

    async def load_form(self, payload, obtaining_type_id):
        snapshot = await self.products.schema_details(payload["category_id"], obtaining_type_id)
        payload["form"] = snapshot["form"]
        payload["category_name"] = snapshot.get("category_name") or payload.get("category_name")
        payload["obtaining_name"] = snapshot.get("obtaining_name") or payload.get("obtaining_name")
        payload["fee_multiplier"] = snapshot["fee_multiplier"]
        payload["category_fee_multiplier"] = snapshot["category_fee_multiplier"]
        payload["obtaining_fee_multiplier"] = snapshot["obtaining_fee_multiplier"]
        payload["fee_checked_at"] = snapshot["checked_at"]
        payload["fee_source"] = "https://playerok.com/sell; live GamePageCategory/gameCategoryObtainingTypes"
        payload["field_index"] = 0
        payload["conditions"] = snapshot["conditions"]
        payload["conditions_page"] = 0
        if snapshot["conditions"].get("automatic"):
            if snapshot["conditions"].get("auto_confirmed"):
                payload["conditions_ack"] = snapshot["conditions"]["hash"]
                payload["step"] = payload.pop("conditions_return", "fields")
            else:
                payload["step"] = "agreements_wait"
        else:
            payload["step"] = "conditions" if snapshot["conditions"]["documents"] else "fields"

    def condition_pages(self, payload):
        documents = payload.get("conditions", {}).get("documents", [])
        return [
            page
            for document in documents
            for page in html_pages(document["title"] + "\n\n" + document["text"])
        ] or ["Условия не предоставлены Playerok."]

    def preview_pages(self, payload, calculation):
        lines = [
            "Игра: " + str(payload.get("game_name") or payload.get("game_id") or "не выбрана"),
            "Категория: " + str(payload.get("category_name") or payload.get("category_id") or "не выбрана"),
            "Способ передачи: "
            + str(
                payload.get("obtaining_name")
                or (
                    "не требуется"
                    if payload.get("obtaining_not_required")
                    else payload.get("obtaining_type_id") or "не выбран"
                )
            ),
            "",
            "Название: " + payload.get("name", ""),
            "Описание:\n" + payload.get("description", ""),
            "",
            "Характеристики:",
        ]
        for field in payload.get("form", []):
            value = payload.get("fields", {}).get(field["key"], "")
            if field.get("data_field") or field.get("comment"):
                shown = "задано (содержимое скрыто)" if value else "не задано"
            else:
                labels = {
                    str(option["value"]): str(option.get("label") or option["value"])
                    for option in field.get("options", [])
                }
                shown = (
                    ", ".join(labels.get(str(v), str(v)) for v in value)
                    if isinstance(value, list)
                    else labels.get(str(value), str(value))
                )
                if not shown:
                    shown = "не задано"
            lines.append(str(field["label"]) + (" *" if field["required"] else "") + ": " + shown)
        lines += [
            "",
            f"Базовая цена: {payload.get('price')} ₽",
            "Скидка: устанавливается отдельным действием после создания серверного черновика.",
            f"Фотографий: {len(payload.get('photos', []))}",
        ]
        mode = payload.get("delivery", "manual")
        lines.append(
            "Режим выдачи: "
            + {
                "manual": "Ручной",
                "text": "Постоянный текст",
                "stock": "Уникальный запас",
                "gifts": "Telegram Gifts",
                "playerok": "Товар выдаёт Playerok; бот не отправляет его повторно",
            }[mode]
        )
        if mode == "stock":
            if payload.get("delivery_data"):
                values, empty, duplicates = stock_values(payload["delivery_data"])
                lines.append(
                    f"Подготовленный запас: {len(values)} уникальных единиц; пустых строк {empty}, повторов {duplicates}. Содержимое скрыто."
                )
            else:
                lines.append("Подготовленный запас пуст.")
            lines.append("Запас будет подготовлен при итоговом запуске. Повторно вводить его не нужно.")
        elif mode == "text":
            lines.append(
                "Текст выдачи: " + ("подготовлен (скрыт)" if payload.get("delivery_data") else "не задан")
            )
        elif mode == "gifts" and payload.get("gift_bundle"):
            from app.gifts.core import gift_name

            lines.append(
                "Подарки: " + ", ".join(gift_name(entry["code"]) for entry in payload["gift_bundle"])
            )
            lines.append(
                f"Стоимость пакета по проверенному каталогу: {sum(entry['stars'] for entry in payload['gift_bundle'])} ⭐"
            )
        lines += [
            "",
            calculation,
            "Актуальный бесплатный или PREMIUM-тариф выберете перед итоговой публикацией.",
            "Сейчас создаётся закрытый серверный черновик. Выдача и публикация подтверждаются в едином экране запуска.",
        ]
        return html_pages("\n".join(lines), 2800)

    async def screen(self, owner, state):
        data = await state.get_data()
        if not data.get("draft_id"):
            raise ValidationError("Черновик не выбран. Откройте локальные черновики или создайте товар")
        draft = self.store.get_draft(data["draft_id"], owner)
        payload = draft["payload"]
        if payload.get("launch_target"):
            raise ValidationError("Это настройка запуска. Откройте товар и подготовку к публикации")
        if (
            payload.get("step") == "conditions"
            and getattr(self.api.account, "auto_accept_agreements", False)
            and not payload.get("manual_conditions_view")
        ):
            await self.load_form(payload, payload.get("obtaining_type_id"))
            await state.update_data(revision=draft["revision"])
            await self.save(owner, state, payload)
            return await self.screen(owner, state)
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
                await self.save(owner, state, payload)
                return await self.screen(owner, state)
            text = "📬 <b>Способ получения</b>\nВыберите вариант Playerok."
        elif step == "agreements_wait":
            status = payload["conditions"].get("auto_status", "WAITING")
            reasons = {
                "UNCERTAIN": "Ответ мог потеряться. Выполняется только чтение серверного состояния.",
                "PROCESSING": "Принятие выполняется другим обработчиком.",
                "WAITING_OTHER_METHOD": "Ожидается сверка условий другого способа передачи этой категории.",
                "VERSION_CHANGE_LIMIT": "Условия меняются слишком часто; автоматическая отправка приостановлена.",
                "NETWORK_ERROR": "Сетевая ошибка. Принятие не подтверждено; повторной отправки нет.",
                "VALIDATION_FAILED": "Playerok вернул неподдерживаемые или несогласованные условия.",
                "UNAUTHORIZED_OR_CHALLENGE": "Playerok требует проверить авторизацию; обход не выполняется.",
            }
            text = "⏳ Проверка условий Playerok\n" + reasons.get(
                status, "Ожидается серверное подтверждение: " + escaped(status)
            )
            text += (
                "\nПри подтверждении мастер продолжится автоматически. Неизвестное принятие не повторяется."
            )
            rows.append([("🔄 Безопасно проверить", f"w:{nonce}:agreements_check")])
        elif step == "conditions":
            pages = self.condition_pages(payload)
            current = min(max(payload.get("conditions_page", 0), 0), len(pages) - 1)
            text = f"📄 <b>Условия выбранной категории · {current + 1}/{len(pages)}</b>\n\n{pages[current]}"
            navigation = []
            if current:
                navigation.append(("← Условия", f"w:{nonce}:conditions_prev"))
            if current + 1 < len(pages):
                navigation.append(("Условия →", f"w:{nonce}:conditions_next"))
            elif not payload.get("manual_conditions_view"):
                label = (
                    "Принимаю условия на Playerok"
                    if payload["conditions"]["pending"]
                    else "Инструкцию прочитал · далее"
                )
                rows.append([(label, f"w:{nonce}:conditions_done")])
            if payload.get("manual_conditions_view"):
                rows.append([("Вернуться без подтверждений", f"w:{nonce}:conditions_done")])
            if navigation:
                rows.append(navigation)
            rows.append([("🔄 Проверить принятие на Playerok", f"w:{nonce}:conditions_refresh")])
            if payload["conditions"]["pending"] and not payload.get("manual_conditions_view"):
                text += "\n\nPlayerok требует принять условия. Только явное подтверждение отправит отдельную операцию; создание товара здесь не выполняется."
        elif step == "fields":
            form = payload["form"]
            index = payload.get("field_index", 0)
            if index >= len(form):
                payload["step"] = (
                    "preview" if payload.get("edit_target") or payload.get("return_preview") else "name"
                )
                await self.save(owner, state, payload)
                return await self.screen(owner, state)
            field = form[index]
            text = f"🧩 <b>{escape(field['label'])}</b>\n" + (
                "Обязательное поле" if field["required"] else "Необязательное: введите − для пропуска"
            )
            if payload.get("edit_target") or payload.get("return_preview"):
                current_value = payload["fields"].get(field["key"], "")
                text += "\nТекущее значение: " + (
                    "сохранено, содержимое скрыто"
                    if field.get("data_field") or field.get("comment")
                    else escaped(current_value, 600)
                    if current_value != ""
                    else "не задано"
                )
                try:
                    validate_field(field, current_value)
                except ValidationError:
                    pass
                else:
                    rows.append([("Оставить текущее значение", f"w:{nonce}:field_keep")])
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
            text = (
                "📝 <b>Название</b>\nВведите название от 10 до 66 символов UTF-16. Ещё 6 оставлены для постоянного кода [XXX]; emoji могут занимать два символа."
                if not payload.get("edit_target") and not payload.get("permanent_code")
                else "📝 <b>Название</b>\nДо 72 символов UTF-16 вместе с постоянным кодом. Код товара сохраняется."
            )
        elif step == "description":
            text = "📋 <b>Описание</b>\nВведите многострочный текст от 10 до 2048 символов."
            from app.gifts.settings import GiftSettings

            gift_settings = getattr(self.api, "gift_settings", None) or GiftSettings.load()
            if (
                payload.get("category_id") == gift_settings.category_id
                and payload.get("obtaining_type_id") == gift_settings.obtaining_type_id
            ):
                text += "\nНапишите обычное описание для покупателя. Подарки выберете кнопками; служебный код добавится автоматически."
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
            text = "💰 <b>Базовая цена</b>\nВведите целую сумму в рублях. Настоящая скидка устанавливается отдельным подтверждённым действием после создания серверного черновика."
        elif step == "delivery":
            text = "⚡ <b>Выдача</b>\nВыберите способ. Полную настройку и проверку завершим на неопубликованном черновике до итогового запуска."
            choices = [
                {"name": "Ручная", "value": "manual"},
                {"name": "Постоянный текст", "value": "text"},
                {"name": "Уникальные значения", "value": "stock"},
                {"name": "Telegram Gifts", "value": "gifts"},
                {"name": "📦 Выдаёт Playerok", "value": "playerok"},
            ]
        elif step == "gift_picker":
            from app.gifts.core import gift_name

            chosen = payload.get("gift_codes", [])
            text = "🎁 <b>Выберите подарки</b>\nПовторное нажатие добавляет ещё один подарок."
            if chosen:
                text += "\n" + escaped(", ".join(gift_name(code) for code in chosen), 600)
            for gift in self.store.rows(
                "SELECT code,stars FROM gift_catalog WHERE available=1 ORDER BY code"
            ):
                rows.append(
                    [
                        (
                            f"{gift_name(gift['code'])} · {gift['stars']} ⭐",
                            f"w:{nonce}:gift_add:" + gift["code"],
                        )
                    ]
                )
            if chosen:
                rows.append(
                    [("Убрать последний", f"w:{nonce}:gift_undo"), ("✅ Выбрать", f"w:{nonce}:gift_done")]
                )
        elif step == "delivery_data":
            text = "🔐 Пришлите текст выдачи или уникальные значения (одно на строку). Данные сохраняются только в локальной БД."
        elif step == "confirm":
            text = "✅ <b>Завершение заказа</b>\nСпособ завершения настроите перед публикацией. Покупатель подтверждает получение отдельно."
            rows = [[("Продолжить", f"w:{nonce}:confirm_done")]]
        elif step == "finance":
            snapshot = payload.get("withdrawal") or {}
            method = (snapshot.get("provider") or {}).get("name") or "не выбран"
            text = (
                f"💰 <b>Финансовые параметры</b>\nСпособ вывода: {escape(method)}\n"
                f"Себестоимость: {escape(str(payload.get('cost', 'не задана')))} ₽\n\n"
                "Расчёт вывода относится к отдельному выводу суммы этой продажи. Для небольших сумм может потребоваться накопить баланс до минимума. Для USDT показан рублёвый эквивалент."
            )
            rows = [
                [("💳 Выбрать способ вывода", f"w:{nonce}:finance_method")],
                [
                    ("✏️ Себестоимость", f"w:{nonce}:finance_cost"),
                    ("Очистить", f"w:{nonce}:finance_clear_cost"),
                ],
                [("🔄 Обновить комиссии", f"w:{nonce}:finance_refresh")],
                [("Без выбранного способа", f"w:{nonce}:finance_clear_method")],
                [("Вернуться к превью", f"w:{nonce}:finance_done")],
            ]
        elif step == "finance_method":
            choices = await withdrawal_methods(self.api)
            text = "💳 <b>Способ предполагаемого вывода</b>\nВыберите доступный вариант Playerok. Это только расчёт, деньги не выводятся."
        elif step == "finance_cost":
            text = "💰 <b>Себестоимость одной единицы</b>\nВведите неотрицательную сумму в рублях, можно с копейками. Она сохраняется только в этом локальном черновике."
        elif step == "preview":
            if payload.get("edit_target"):
                text = (
                    f"🧩 <b>Редактирование: {escaped(payload['name'], 400)}</b>\n"
                    f"Полей по схеме: {len(payload['form'])}\n"
                    "Будут изменены только атрибуты и данные товара. Приватные значения в превью не выводятся.\n"
                    "Перед отправкой проверяются владение, текущие поля и условия Playerok."
                )
                rows = [
                    [("✅ Сохранить изменения на Playerok", f"w:{nonce}:submit")],
                    [("✏️ Изменить поля", f"w:{nonce}:edit:fields")],
                    [("📄 Условия", f"w:{nonce}:conditions_refresh")],
                ]
            else:
                result = draft_calculation(payload)
                save_quote(self.store, owner, "draft:" + draft["id"], payload, result)
                calculation = format_calculation(result)
                pages = self.preview_pages(payload, calculation)
                current = min(max(payload.get("preview_page", 0), 0), len(pages) - 1)
                text = f"📦 <b>Полное превью · {current + 1}/{len(pages)}</b>\n\n" + pages[current]
                rows = [
                    [("Продолжить настройку", f"w:{nonce}:submit")],
                    [("📝 Название", f"w:{nonce}:edit:name"), ("📋 Описание", f"w:{nonce}:edit:description")],
                    [("🖼 Фото", f"w:{nonce}:edit:photos"), ("💰 Цена", f"w:{nonce}:edit:price")],
                    [("⚡ Выдача", f"w:{nonce}:edit:delivery")],
                    [("🎮 Игра", f"w:{nonce}:edit:game"), ("📂 Категория", f"w:{nonce}:edit:category")],
                    [
                        ("📬 Способ передачи", f"w:{nonce}:edit:obtaining"),
                        ("🧩 Характеристики", f"w:{nonce}:edit:fields"),
                    ],
                    [("📄 Условия категории", f"w:{nonce}:conditions_refresh")],
                    [("💰 Вывод и себестоимость", f"w:{nonce}:finance_open")],
                ]
                nav = []
                if current:
                    nav.append(("← Превью", f"w:{nonce}:preview_page:{current - 1}"))
                if current + 1 < len(pages):
                    nav.append(("Превью →", f"w:{nonce}:preview_page:{current + 1}"))
                if nav:
                    rows.insert(0, nav)
                for start in range(0, len(payload["photos"]), 3):
                    rows.insert(
                        1 + start // 3,
                        [
                            (f"🖼 Фото {i + 1}", f"w:{nonce}:photo_preview:{i}")
                            for i in range(start, min(start + 3, len(payload["photos"])))
                        ],
                    )
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
        if step not in {"game", "preview", "agreements_wait"}:
            rows.append([("⬅️ Назад", f"w:{nonce}:back")])
        if step in {"name", "description", "price"} and payload.get(step) is not None:
            text += "\nСохранённое значение: " + escaped(payload[step], 2300)
        if payload.get("return_preview") and step != "preview":
            rows.append([("Вернуться к превью", f"w:{nonce}:preview_return")])
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
        if (
            action == "conditions_done"
            and step == "conditions"
            and payload.pop("manual_conditions_view", False)
        ):
            payload["step"] = payload.pop("conditions_return", "preview")
        elif (
            action == "agreements_check"
            and step == "agreements_wait"
            or action == "conditions_done"
            and step == "conditions"
            and getattr(self.api.account, "auto_accept_agreements", False)
        ):
            await self.load_form(payload, payload.get("obtaining_type_id"))
        elif action == "preview_page" and step == "preview":
            number = int(parts[3])
            pages = self.preview_pages(payload, format_calculation(draft_calculation(payload)))
            if not 0 <= number < len(pages):
                raise ValidationError("Страница превью недоступна")
            payload["preview_page"] = number
        elif action == "preview_return" and payload.get("return_preview"):
            if not payload.get("category_id") or (
                not payload.get("obtaining_type_id") and not payload.get("obtaining_not_required")
            ):
                raise ValidationError("Завершите выбор категории и способа передачи")
            snapshot = await self.products.schema_details(
                payload["category_id"], payload.get("obtaining_type_id")
            )
            validate_draft(payload, snapshot["form"], getattr(self.api.account, "role", None))
            require_conditions(payload, snapshot["conditions"])
            payload["step"] = "preview"
        elif action == "finance_open" and step == "preview" and not payload.get("edit_target"):
            payload["step"] = "finance"
        elif (
            action
            in {
                "finance_method",
                "finance_cost",
                "finance_done",
                "finance_clear_cost",
                "finance_clear_method",
                "finance_refresh",
            }
            and step == "finance"
        ):
            if action == "finance_method":
                payload["step"] = "finance_method"
            elif action == "finance_cost":
                payload["step"] = "finance_cost"
            elif action == "finance_done":
                payload["step"] = "preview"
            elif action == "finance_clear_cost":
                payload.pop("cost", None)
            elif action == "finance_clear_method":
                payload.pop("withdrawal", None)
            else:
                snapshot = await self.products.schema_details(
                    payload["category_id"], payload.get("obtaining_type_id")
                )
                payload["fee_multiplier"] = snapshot["fee_multiplier"]
                payload["fee_checked_at"] = snapshot["checked_at"]
                payload["category_fee_multiplier"] = snapshot["category_fee_multiplier"]
                payload["obtaining_fee_multiplier"] = snapshot["obtaining_fee_multiplier"]
                if payload.get("withdrawal"):
                    payload["withdrawal"] = await withdrawal_snapshot(
                        self.api, payload["withdrawal"]["provider"]["id"]
                    )
        elif action in {"conditions_next", "conditions_prev", "conditions_done"} and step == "conditions":
            pages = self.condition_pages(payload)
            current = payload.get("conditions_page", 0)
            if action == "conditions_next":
                if current + 1 >= len(pages):
                    raise ValidationError("Последняя страница условий уже открыта")
                payload["conditions_page"] = current + 1
            elif action == "conditions_prev":
                if current <= 0:
                    raise ValidationError("Первая страница условий уже открыта")
                payload["conditions_page"] = current - 1
            else:
                if current != len(pages) - 1:
                    raise ValidationError("Сначала откройте все страницы условий")
                conditions = payload["conditions"]
                if conditions["pending"]:
                    if not self.api.account.writes_enabled:
                        raise ValidationError("Записи Playerok отключены. Условия на сервере не приняты.")
                    account_id = getattr(self.api.account, "id", None)
                    if not account_id:
                        raise ValidationError("Авторизация Playerok не подтверждена")
                    acceptance = {
                        "agreements": conditions["pending"],
                        "account_id": account_id,
                        "obtaining_type_id": payload.get("obtaining_type_id"),
                    }
                    ident = self.store.enqueue(
                        "agreements:" + fingerprint({"category": payload["category_id"], **acceptance}),
                        "agreements",
                        payload["category_id"],
                        acceptance,
                    )
                    self.store.audit(owner, "ACCEPT_AGREEMENTS_QUEUED", payload["category_id"], "PENDING")
                    return (
                        f"Принятие условий поставлено в очередь: <code>{ident}</code>. Затем нажмите «Проверить принятие на Playerok».",
                        keyboard([[("Проверить принятие на Playerok", f"w:{parts[1]}:conditions_refresh")]]),
                    )
                payload["conditions_ack"] = conditions["hash"]
                self.store.audit(owner, "CONDITIONS_REVIEWED", payload["category_id"], conditions["hash"])
                payload["step"] = payload.pop("conditions_return", "fields")
        elif action == "conditions_refresh" and step in {"conditions", "preview"}:
            if getattr(self.api.account, "auto_accept_agreements", False):
                snapshot = await self.products.schema_details(
                    payload["category_id"], payload.get("obtaining_type_id"), auto_agreements=False
                )
                payload.update(
                    conditions=snapshot["conditions"],
                    conditions_page=0,
                    manual_conditions_view=True,
                    conditions_return=payload.get("conditions_return", step),
                    step="conditions",
                )
            else:
                if step == "preview":
                    payload["conditions_return"] = "preview"
                await self.load_form(payload, payload.get("obtaining_type_id"))
                if not payload["conditions"]["documents"]:
                    payload["step"] = payload.pop("conditions_return", "fields")
        elif action == "choice_page":
            try:
                number = int(parts[3])
                if not 0 <= number < data.get("choice_pages", 0):
                    raise ValueError
            except (ValueError, IndexError):
                raise ValidationError("Страница вариантов недоступна") from None
            payload["choice_page"] = {"view": data["choice_view"], "number": number}
        elif action in {"gift_add", "gift_undo", "gift_done"} and step == "gift_picker":
            from app.services.logical_products import LogicalProducts

            gifts = LogicalProducts(self.store, self.api).gift_service()
            chosen = list(payload.get("gift_codes", []))
            if action == "gift_add":
                if len(chosen) >= gifts.settings.max_gifts:
                    raise ValidationError("Достигнут предел подарков в одном заказе")
                gifts.catalog_bundle(parts[3])
                chosen.append(parts[3])
            elif action == "gift_undo":
                chosen = chosen[:-1]
            else:
                if not chosen:
                    raise ValidationError("Выберите хотя бы один подарок")
                payload["gift_code"] = ", ".join(chosen)
                payload["gift_bundle"] = gifts.catalog_bundle(payload["gift_code"])
                payload["step"] = "preview"
            payload["gift_codes"] = chosen
        elif action == "back":
            if step == "fields" and payload.get("field_index", 0) > 0:
                payload["field_index"] -= 1
            else:
                previous = {
                    "category": "game",
                    "obtaining": "category",
                    "fields": "obtaining",
                    "conditions": "category" if payload.get("obtaining_not_required") else "obtaining",
                    "name": "fields",
                    "description": "name",
                    "photos": "description",
                    "price": "photos",
                    "delivery": "price",
                    "delivery_data": "delivery",
                    "gift_picker": "delivery",
                    "confirm": "delivery",
                    "finance": "preview",
                    "finance_method": "finance",
                    "finance_cost": "finance",
                }
                if step not in previous:
                    raise ValidationError("Предыдущий шаг недоступен")
                payload["step"] = previous[step]
                if step in {"name", "fields"} and (not payload.get("form") or step == "fields"):
                    payload["step"] = (
                        "conditions"
                        if payload.get("conditions", {}).get("documents")
                        and not getattr(self.api.account, "auto_accept_agreements", False)
                        else "category"
                        if payload.get("obtaining_not_required")
                        else "obtaining"
                    )
                    payload["conditions_page"] = 0
                if payload["step"] == "fields":
                    payload["field_index"] = max(0, len(payload.get("form", [])) - 1)
                if payload.get("edit_target") and step in {"fields", "conditions"}:
                    payload["step"] = "preview"
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
                        "obtaining_name",
                        "conditions_ack",
                    ):
                        payload.pop(dependent, None)
                    payload["fields"] = {}
                    if step == "game":
                        payload.pop("category_id", None)
                        payload.pop("category_name", None)
                payload[key], payload["step"] = choice["id"], next_step
                payload[
                    {"game": "game_name", "category": "category_name", "obtaining": "obtaining_name"}[step]
                ] = choice.get("name") or choice["id"]
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
                payload["managed_delivery"] = choice["value"] in {"text", "stock", "gifts", "playerok"}
                if choice["value"] == "gifts":
                    from app.gifts.core import gift_code_from_description
                    from app.services.logical_products import LogicalProducts

                    gifts = LogicalProducts(self.store, self.api).gift_service()
                    await gifts.refresh()
                    previous = gift_code_from_description(
                        payload.get("description", ""), required=False, max_count=gifts.settings.max_gifts
                    )
                    if previous:
                        payload["gift_code"] = previous
                        payload["gift_codes"] = previous.split(", ")
                        payload["gift_bundle"] = gifts.catalog_bundle(previous)
                        payload["description"] = self.gift_description(
                            payload["description"], previous, gifts.settings.max_gifts
                        )
                    else:
                        payload["step"] = "gift_picker"
                        await self.save(owner, state, payload)
                        return await self.screen(owner, state)
                payload["step"] = (
                    "preview" if choice["value"] in {"manual", "gifts", "playerok"} else "delivery_data"
                )
            elif step == "finance_method":
                payload["withdrawal"] = await withdrawal_snapshot(self.api, choice["id"])
                payload["step"] = "finance"
            else:
                raise ValidationError("Выбор не соответствует шагу")
        elif (
            action == "field_keep"
            and step == "fields"
            and (payload.get("edit_target") or payload.get("return_preview"))
        ):
            field = payload["form"][payload["field_index"]]
            validate_field(field, payload["fields"].get(field["key"], ""))
            payload["field_index"] += 1
        elif action == "field_done" and step == "fields":
            field = payload["form"][payload["field_index"]]
            if field["kind"] != "multi":
                raise ValidationError("Подтверждение не соответствует текущему полю")
            payload["fields"][field["key"]] = validate_field(field, payload["fields"].get(field["key"], []))
            payload["field_index"] += 1
        elif action == "photos_done" and step == "photos":
            payload.pop("replace_photo_index", None)
            payload["step"] = "preview" if payload.get("return_preview") else "price"
        elif action in {"photo_remove", "photo_replace", "photo_preview"} and (
            step == "photos" or (step == "preview" and action == "photo_preview")
        ):
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
                    BufferedInputFile(image, filename="preview.jpg"),
                    caption=f"Фото {index + 1}",
                )
                return await self.screen(owner, state)
            if action == "photo_replace":
                payload["replace_photo_index"] = index
            else:
                removed_photo = payload["photos"].pop(index)
                # Validate cleanup scope before committing a changed draft.
                self.attachments.path(removed_photo)
                payload.pop("replace_photo_index", None)
        elif action == "confirm_done" and step in {"confirm", "preview"}:
            payload["step"] = "preview"
        elif action == "edit" and step == "preview":
            target = parts[3] if len(parts) > 3 else "name"
            allowed = (
                {"fields"}
                if payload.get("edit_target")
                else {
                    "name",
                    "description",
                    "photos",
                    "price",
                    "delivery",
                    "game",
                    "category",
                    "obtaining",
                    "fields",
                }
            )
            if target not in allowed:
                raise ValidationError("Неизвестное поле редактирования")
            payload["step"] = target
            payload["return_preview"] = True
            payload["preview_page"] = 0
            if target == "fields":
                payload["field_index"] = 0
        elif action == "submit" and step == "preview":
            if payload.get("delivery") == "gifts":
                self.validate_gift(payload)
            if not self.api.account.writes_enabled:
                raise ValidationError("Записи Playerok отключены в .env. Локальный черновик сохранён.")
            method = (
                self.products.queue_fields_edit if payload.get("edit_target") else self.products.queue_create
            )
            ident = await method(draft["id"], owner, draft["revision"])
            await state.clear()
            return (
                "⏳ Ожидает выполнения: подготовка товара на Playerok. Продажи ещё не открыты. Настройка выдачи и публикации продолжится здесь.",
                keyboard(
                    [
                        [("Открыть результат", "result:show:" + ident)],
                        [("Продолжить настройку и запуск", "launch:from:" + draft["id"])],
                    ]
                    if not payload.get("edit_target")
                    else [[("Открыть результат", "result:show:" + ident)]]
                ),
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
            if step == "name" and not payload.get("edit_target"):
                if payload.get("permanent_code"):
                    payload[step] = titled(payload[step], payload["permanent_code"])
                elif title_code(payload[step]):
                    raise ValidationError(
                        "Введите название без [XXX]: постоянный код назначится автоматически"
                    )
                elif text_units(payload[step]) > 66:
                    raise ValidationError("Название: максимум 66 символов UTF-16, ещё 6 нужны для кода [XXX]")
            if step == "description" and any(
                line.strip().lower().startswith("gift:") for line in text.splitlines()
            ):
                self.validate_gift(payload)
                if payload.get("managed_delivery") and payload.get("delivery") == "gifts":
                    from app.services.logical_products import LogicalProducts

                    payload["description"] = self.gift_description(
                        payload["description"],
                        payload["gift_code"],
                        LogicalProducts(self.store, self.api).gift_service().settings.max_gifts,
                    )
            payload["step"] = (
                "preview" if payload.get("return_preview") else "description" if step == "name" else "photos"
            )
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
            payload["step"] = "preview" if payload.get("return_preview") else "delivery"
        elif step == "finance_cost":
            payload["cost"] = str(money(text.replace(",", ".")))
            payload["step"] = "finance"
        elif step == "delivery_data":
            if not text.strip() or len(text) > 10000:
                raise ValidationError("Введите непустые данные до 10000 символов")
            if payload["delivery"] == "stock":
                stock_values(text)
            elif len(text.encode("utf-16-le")) // 2 > 3500:
                raise ValidationError("Текст выдачи: до 3500 символов")
            payload["delivery_data"] = text
            payload["step"] = "preview"
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
