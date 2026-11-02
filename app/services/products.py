import uuid
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from app.database.store import Conflict, now
from app.services.agreements import Agreements, fingerprint, require_conditions
from app.services.finance import selected_fee_multiplier
from app.services.products_errors import ValidationError


def text_units(value):
    return len(value.encode("utf-16-le")) // 2


def listing_text(value, field):
    # Current official sell frontend, module 85570 (2026-10-29).
    limits = {"name": (10, 72), "description": (10, 2048)}
    if field not in limits or not isinstance(value, str):
        raise ValidationError("Некорректное текстовое поле товара")
    value = value.strip()
    minimum, maximum = limits[field]
    if not minimum <= text_units(value) <= maximum:
        label = "Название" if field == "name" else "Описание"
        raise ValidationError(
            f"{label}: требуется от {minimum} до {maximum} символов по правилам формы Playerok"
        )
    return value


def listing_price(value, role=None):
    price = integer_price(value)
    minimum = 10 if role == "OFFICIAL_SELLER" else 90
    if not minimum <= price <= 100_000:
        raise ValidationError(f"Цена по текущей форме Playerok: от {minimum} до 100000 ₽")
    return price


def integer_price(value):
    try:
        price = Decimal(str(value).replace(",", "."))
    except InvalidOperation:
        raise ValidationError("Введите цену числом") from None
    if not price.is_finite() or price <= 0 or price != price.to_integral_value() or price > 1_000_000_000:
        raise ValidationError("Требуется положительная целая цена в рублях")
    return int(price)


def category_form(category, data_fields):
    groups = defaultdict(list)
    for option in category.get("options") or []:
        if not option.get("field"):
            raise ValidationError("API вернул атрибут без field")
        groups[option["field"]].append(option)
    fields = []
    for name, options in groups.items():
        if any(o.get("multiple") for o in options):
            kind = "multi"
        else:
            types = {o.get("type") for o in options}
            if types <= {"SELECTOR", "SWITCH"}:
                kind = "choice"
            elif types == {"RANGE"}:
                kind = "number"
            else:
                raise ValidationError("Неподдерживаемый тип атрибута категории")
        fields.append(
            {
                "key": name,
                "label": options[0].get("group") or name,
                "kind": kind,
                "required": True,
                "options": options,
            }
        )
    seller_fields = [field for field in data_fields if field.get("type") == "ITEM_DATA"]
    for field in seller_fields:
        if field.get("type") != "ITEM_DATA":
            continue  # Buyer's obtaining fields must never be requested as seller data.
        input_type = field.get("input_type")
        if input_type not in {"INPUT", "TEXT", "TEXTAREA", "NUMBER", "INTEGER"}:
            raise ValidationError("Неподдерживаемый тип обязательной схемы: публикация заблокирована")
        fields.append(
            {
                "key": field["id"],
                "label": field.get("label") or field["id"],
                "kind": "number" if input_type in {"NUMBER", "INTEGER"} else "text",
                "required": bool(field.get("required")),
                "data_field": True,
                "min_length": 2,
                "max_length": 2048,
            }
        )
    if not seller_fields:
        # Official sell form modules 66572/85570 and 25.js send textData as
        # input.comment when no structured seller fields are defined.
        fields.append(
            {
                "key": "__comment",
                "label": "Данные для покупателя после покупки",
                "kind": "text",
                "required": True,
                "comment": True,
                "min_length": 2,
                "max_length": 2048,
            }
        )
    return fields


def validate_field(field, value):
    text = str(value).strip()
    if not text:
        if field["required"]:
            raise ValidationError("Поле обязательно")
        return ""
    if field["kind"] in {"choice", "multi"}:
        options = {str(o["value"]): o["value"] for o in field["options"]}
        if field["kind"] == "multi":
            selected = (
                [str(v).strip() for v in value]
                if isinstance(value, list)
                else [v.strip() for v in text.split(",")]
            )
        else:
            selected = [text]
        if not selected and field["required"]:
            raise ValidationError("Выберите хотя бы одно значение")
        if any(v not in options for v in selected):
            raise ValidationError("Выберите значение из списка API")
        return [options[v] for v in selected] if field["kind"] == "multi" else options[selected[0]]
    if field["kind"] == "number":
        try:
            number = Decimal(text)
        except InvalidOperation:
            raise ValidationError("Введите число") from None
        if not number.is_finite():
            raise ValidationError("Недопустимое число")
        for option in field.get("options", []):
            limits = option.get("value_range_limit") or {}
            if limits.get("min") is not None and number < Decimal(str(limits["min"])):
                raise ValidationError("Число меньше минимума API")
            if limits.get("max") is not None and number > Decimal(str(limits["max"])):
                raise ValidationError("Число больше максимума API")
        return text
    if text_units(text) < field.get("min_length", 0):
        raise ValidationError("Значение слишком короткое")
    if text_units(text) > field.get("max_length", 10_000):
        raise ValidationError("Слишком длинное значение")
    return text


def validate_draft(payload, form, role=None):
    for key in ["category_id", "name", "description"]:
        if not str(payload.get(key) or "").strip():
            raise ValidationError(f"Не заполнено поле {key}")
    if not payload.get("obtaining_type_id") and not payload.get("obtaining_not_required"):
        raise ValidationError("Способ получения не выбран")
    listing_text(payload["name"], "name")
    listing_text(payload["description"], "description")
    listing_price(payload.get("price", ""), role)
    values = payload.get("fields", {})
    for field in form:
        validate_field(field, values.get(field["key"], ""))
    if payload.get("discount") is not None:
        raise ValidationError(
            "Скидка применяется отдельным подтверждённым действием после создания серверного черновика"
        )


def save_image(data, directory, max_size=10_000_000):
    if not data or len(data) > max_size:
        raise ValidationError("Файл пуст или превышает локальный лимит 10 МБ")
    try:
        with Image.open(BytesIO(data)) as image:
            if image.format not in {"JPEG", "PNG", "WEBP"}:
                raise ValidationError("Поддерживаются JPEG, PNG и WEBP")
            if image.width * image.height > 25_000_000:
                raise ValidationError("Слишком большое разрешение")
            image.verify()
        with Image.open(BytesIO(data)) as image:
            image = image.convert("RGB")
            path = Path(directory) / (uuid.uuid4().hex + ".jpg")
            path.parent.mkdir(parents=True, exist_ok=True)
            image.save(path, "JPEG", quality=92)
            if path.stat().st_size > max_size:
                path.unlink()
                raise ValidationError("Изображение после обработки превышает лимит размера")
            return str(path.resolve())
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise ValidationError("Повреждённое или неподдерживаемое изображение") from None


def field_baseline(item):
    return fingerprint(
        {key: item.get(key) for key in ("attributes", "data_fields", "comment", "category", "obtaining_type")}
    )


def priority_price_basis(item):
    if item.get("raw_price") is None:
        raise ValidationError("API не вернул базовую цену для расчёта размещения")
    return integer_price(item["raw_price"])


class Products:
    def __init__(self, store, api):
        self.store, self.api = store, api

    def native_relist_guard(self, value):
        from app.services.logical_products import LogicalProducts

        logical = LogicalProducts(self.store, self.api)
        logical.authority(value["actor"], value["account_id"], writes=True)
        binding = logical.listing(value["item_id"])
        if binding and (
            len(binding["code"]) != 3 or binding["state"] != "VERIFIED" or not binding["verified_at"]
        ):
            raise Conflict("Сначала подтвердите код в названии. Неперенесённый архив публиковать небезопасно")

    async def native_relist_preview(self, owner, item_id, quote_id):
        from app.services.automation import Automation
        from app.services.logical_products import LogicalProducts

        LogicalProducts(self.store, self.api).authority(owner)
        item = await Automation(self.store, self.api).item(owner, item_id)
        binding = LogicalProducts(self.store, self.api).listing(item_id)
        if binding:
            LogicalProducts(self.store, self.api).verify_listing(binding, item)
        if item.get("status") != "SOLD" or item.get("may_be_published") is not True:
            raise ValidationError("Playerok не разрешил повторное размещение этого товара")
        quotes = await self.priority_preview(item_id, priority_price_basis(item))
        selected = [q for q in quotes if q.get("id") == quote_id and q.get("type") in {"DEFAULT", "PREMIUM"}]
        if len(selected) != 1 or selected[0].get("price") is None:
            raise ValidationError("Тариф повторного размещения не подтверждён")
        quote = selected[0]
        if quote["type"] == "PREMIUM":
            from app.services.paid_promotions import PaidPromotions

            return await PaidPromotions(self.store, self.api).preview(
                owner, item_id, quote_id, native_relist=True
            )
        if Decimal(str(quote["price"])) != 0:
            raise ValidationError("Обычное размещение не подтверждено как бесплатное")
        return {
            "native_relist": True,
            "actor": owner,
            "account_id": self.api.account.id,
            "priority_id": quote["id"],
            "price": item["price"],
            "raw_price": priority_price_basis(item),
            "item_id": item_id,
        }

    async def queue_native_relist(self, owner, value, key):
        if value.get("type") == "PREMIUM":
            from app.services.paid_promotions import PaidPromotions

            return await PaidPromotions(self.store, self.api).queue(owner, value, key, manual=True)
        fresh = await self.native_relist_preview(owner, value["item_id"], value["priority_id"])
        if fresh != value:
            raise Conflict("Товар или тариф изменились. Подтвердите заново")
        self.native_relist_guard(fresh)
        return self.store.enqueue(key, "publish", value["item_id"], fresh, actor=owner)

    async def schema(self, category_id, obtaining_type_id):
        return (await self.schema_details(category_id, obtaining_type_id))["form"]

    async def schema_details(self, category_id, obtaining_type_id, *, auto_agreements=True):
        category = await self.api.call("get_game_category", id=category_id)
        if getattr(self.api.account, "auto_accept_agreements", False) and category.get("id") != category_id:
            raise ValidationError("Playerok вернул другую категорию")
        obtaining = []
        async for page in self.api.pages(
            "get_game_category_obtaining_types", "obtaining_types", game_category_id=category_id
        ):
            obtaining.extend(page)
        if obtaining and obtaining_type_id not in {o["id"] for o in obtaining}:
            raise ValidationError("Способ получения не принадлежит текущей категории")
        if not obtaining and obtaining_type_id is not None:
            raise ValidationError("У категории отсутствуют способы получения")
        fields = []
        async for page in self.api.pages(
            "get_game_category_data_fields",
            "data_fields",
            game_category_id=category_id,
            obtaining_type_id=obtaining_type_id,
        ):
            fields.extend(page)
        selected = next((entry for entry in obtaining if entry["id"] == obtaining_type_id), {})
        if getattr(self.api.account, "auto_accept_agreements", False) and (
            len({entry["id"] for entry in obtaining}) != len(obtaining)
            or selected.get("game_category_id") not in {None, category_id}
        ):
            raise ValidationError("Принадлежность способа передачи категории не подтверждена")
        conditions = await Agreements(self.api).snapshot(category, selected)
        if auto_agreements and getattr(self.api.account, "auto_accept_agreements", False):
            from app.services.agreements import AutoAgreements

            conditions = await AutoAgreements(self.store, self.api).ensure(
                category_id, obtaining_type_id, conditions
            )
        return {
            "conditions": conditions,
            "category_name": category.get("name"),
            "obtaining_name": selected.get("name"),
            "form": category_form(category, fields),
            "category_fee_multiplier": category.get("fee_multiplier"),
            "obtaining_fee_multiplier": selected.get("fee_multiplier"),
            "fee_multiplier": selected_fee_multiplier(
                category.get("fee_multiplier"), selected.get("fee_multiplier")
            ),
            "checked_at": now(),
        }

    async def queue_create(self, ident, owner, revision):
        draft = self.store.get_draft(ident, owner)
        if draft["payload"].get("edit_target"):
            raise ValidationError("Черновик редактирования нельзя использовать для создания нового товара")
        details = await self.schema_details(
            draft["payload"]["category_id"], draft["payload"]["obtaining_type_id"]
        )
        require_conditions(draft["payload"], details["conditions"])
        validate_draft(draft["payload"], details["form"], getattr(self.api.account, "role", None))
        from app.services.logical_products import LogicalProducts

        logical = LogicalProducts(self.store, self.api)
        creation = (
            await logical.prepare_creation(owner, draft["payload"])
            if draft["payload"].get("managed_delivery")
            else None
        )
        with self.store.transaction() as db:
            r = db.execute(
                "UPDATE drafts SET state='QUEUED',revision=revision+1 WHERE id=? AND owner_id=? AND revision=? AND state='DRAFT'",
                (ident, owner, revision),
            )
            if r.rowcount != 1:
                raise Conflict("Черновик уже отправлен или изменён")
            if creation:
                logical.commit_creation(db, owner, ident, draft["payload"], creation)
            operation = self.store.enqueue(
                f"create:{ident}", "create", ident, {"owner": owner}, db, actor=owner
            )
            db.execute(
                "INSERT INTO audit_log(actor,action,target,outcome,created_at) VALUES(?,?,?,?,?)",
                (owner, "CREATE_QUEUED", ident, "PENDING", now()),
            )
        return operation

    async def priority_preview(self, item_id, price):
        statuses = await self.api.call(
            "get_item_priority_statuses", item_id=item_id, item_price=integer_price(price)
        )
        if not isinstance(statuses, list):
            raise ValidationError("Цена размещения не получена")
        return statuses

    async def owned_item(self, item_id):
        item = await self.api.call("get_item", id=item_id)
        if (
            not getattr(self.api.account, "id", None)
            or (item.get("user") or {}).get("id") != self.api.account.id
        ):
            raise ValidationError("Владение товаром не подтверждено")
        if item.get("is_editable") is False:
            raise ValidationError("Playerok запрещает редактирование этого товара")
        return item

    async def edit_fields_draft(self, owner, item_id):
        item = await self.owned_item(item_id)
        category_id = (item.get("category") or {}).get("id")
        if not category_id:
            raise ValidationError("API не вернул категорию товара")
        obtaining_id = (item.get("obtaining_type") or {}).get("id")
        details = await self.schema_details(category_id, obtaining_id)
        attributes = item.get("attributes") or {}
        data = {field["id"]: field.get("value") for field in item.get("data_fields") or []}
        values = {}
        for field in details["form"]:
            values[field["key"]] = (
                (item.get("comment") or "")
                if field.get("comment")
                else data.get(field["key"], "")
                if field.get("data_field")
                else attributes.get(field["key"], "")
            )
        payload = {
            "edit_target": item_id,
            "edit_baseline": field_baseline(item),
            "category_id": category_id,
            "obtaining_type_id": obtaining_id,
            "obtaining_not_required": obtaining_id is None,
            "game_id": (item.get("game") or {}).get("id"),
            "fields": values,
            "photos": [],
            "name": item.get("name") or item_id,
            "description": item.get("description") or "",
            "price": item.get("price"),
            "step": "fields",
            "delivery": "manual",
            "auto_confirm": False,
        }
        return self.store.draft(owner, payload)

    async def queue_fields_edit(self, ident, owner, revision):
        draft = self.store.get_draft(ident, owner)
        value = draft["payload"]
        target = value.get("edit_target")
        if not target:
            raise ValidationError("Черновик не привязан к редактируемому товару")
        item = await self.owned_item(target)
        if field_baseline(item) != value["edit_baseline"]:
            raise Conflict("Поля товара изменились на Playerok. Откройте редактирование заново.")
        details = await self.schema_details(value["category_id"], value["obtaining_type_id"])
        require_conditions(value, details["conditions"])
        for field in details["form"]:
            validate_field(field, value["fields"].get(field["key"], ""))
        with self.store.transaction() as db:
            updated = db.execute(
                "UPDATE drafts SET state='QUEUED',revision=revision+1 WHERE id=? AND owner_id=? AND revision=? AND state='DRAFT'",
                (ident, owner, revision),
            )
            if updated.rowcount != 1:
                raise Conflict("Редактирование уже отправлено или изменено")
            operation = self.store.enqueue(
                "fields:" + ident, "edit_fields", target, {"draft_id": ident, "owner": owner}, db, actor=owner
            )
            db.execute(
                "INSERT INTO audit_log(actor,action,target,outcome,created_at) VALUES(?,?,?,?,?)",
                (owner, "FIELDS_EDIT_QUEUED", target, "PENDING", now()),
            )
            return operation

    async def free_priority(self, item_id, price):
        if getattr(getattr(self.api, "account", None), "auto_accept_agreements", False):
            await self.prepare_item_agreements(await self.owned_item(item_id))
        statuses = await self.priority_preview(item_id, price)
        for status in statuses:
            if (
                status.get("type") == "DEFAULT"
                and status.get("price") is not None
                and Decimal(str(status["price"])) == 0
            ):
                return status
        raise ValidationError("Бесплатность размещения не подтверждена API")

    async def prepare_item_agreements(self, item):
        if getattr(self.api.account, "auto_accept_agreements", False):
            category = (item.get("category") or {}).get("id")
            if not category:
                raise ValidationError("Категория лота не подтверждена")
            details = await self.schema_details(category, (item.get("obtaining_type") or {}).get("id"))
            require_conditions({}, details["conditions"])
